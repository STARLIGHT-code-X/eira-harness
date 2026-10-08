"""Pre-approval syntax guard for file edits and optional sandboxed lint feedback.

The guard only parses text with stdlib parsers (ast, json, tomllib) on the
host; it never imports, evaluates or executes file content. Lint commands run
only through Toolbox.shell, so they inherit the Docker sandbox and its policy.
"""
from __future__ import annotations

from dataclasses import dataclass
import ast
import fnmatch
import json
from pathlib import PurePosixPath
import re
import shlex
import sys
import tomllib
import warnings

from .security import HarnessError
from .text import lines

MODES = ("reject", "warn", "off")
# XML is deliberately absent: expat's entity-expansion protection depends on the linked version.
LANGUAGES = {".py": "python", ".pyi": "python", ".json": "json", ".toml": "toml"}
LABELS = {"python": "Python", "json": "JSON", "toml": "TOML"}
EDIT_TOOLS = ("edit_file", "write_file", "apply_patch")
LINT_TIMEOUT = 60
LINT_MAX_PER_CALL = 5
LINT_OUTPUT_CHARS = 4_000
LINE_CHARS = 200
BLOCK_LINES = 40
BLOCK_CHARS = 3_000

_SCOPE = re.compile(r"^(\s*)(async\s+def|def|class)\b")
_TOML_AT = re.compile(r"\s*\(at line (\d+), column (\d+)\)\s*$")
_TOML_END = re.compile(r"\s*\(at end of document\)\s*$")


@dataclass
class Diagnostic:
    line: int
    column: int | None
    message: str


class NotChecked(Exception):
    """The parser could not give an answer (size, recursion or memory limits)."""


def language_of(path: str) -> str | None:
    return LANGUAGES.get(PurePosixPath(str(path)).suffix.lower())


def _end_position(text: str) -> tuple[int, int]:
    rows = lines(text)
    if not rows:
        return 1, 1
    if text.endswith(("\n", "\r")):
        return len(rows) + 1, 1
    return len(rows), len(rows[-1]) + 1


def _parse(language: str, path: str, text: str) -> Diagnostic | None:
    """Return None when text parses, a Diagnostic when it does not; raise NotChecked otherwise."""
    from .tools import MAX_TEXT_FILE  # local import: tools imports this module

    if len(text.encode("utf-8", "surrogatepass")) > MAX_TEXT_FILE:
        raise NotChecked("file is larger than the check limit")
    try:
        if language == "python":
            with warnings.catch_warnings():
                # Invalid escapes and similar warnings must not reach the terminal.
                warnings.simplefilter("ignore")
                ast.parse(text, filename=path)
        elif language == "json":
            json.loads(text)
        else:
            tomllib.loads(text)
    except (RecursionError, MemoryError) as exc:
        raise NotChecked("the parser hit its nesting or memory limit") from exc
    except SyntaxError as exc:  # includes IndentationError and TabError
        return Diagnostic(exc.lineno or 1, exc.offset or None, exc.msg or "invalid syntax")
    except json.JSONDecodeError as exc:
        return Diagnostic(exc.lineno, exc.colno, exc.msg)
    except tomllib.TOMLDecodeError as exc:
        message = str(exc)
        line, column = getattr(exc, "lineno", None), getattr(exc, "colno", None)
        position = _TOML_AT.search(message)
        if position:
            message = message[:position.start()]
            line, column = line or int(position.group(1)), column or int(position.group(2))
        elif _TOML_END.search(message):
            message = _TOML_END.sub("", message)
            line, column = _end_position(text)
        message = getattr(exc, "msg", None) or message
        return Diagnostic(line or 1, column, message)
    except ValueError as exc:  # NUL bytes on older 3.11 patch releases
        return Diagnostic(1, None, str(exc))
    return None


def _width(prefix: str) -> int:
    return len(prefix.expandtabs(8))


def scopes(rows: list[str], line: int) -> list[int]:
    """1-based line numbers of the class/def headers enclosing `line`, outermost first."""
    if not rows:
        return []
    index = min(max(line, 1), len(rows)) - 1
    anchor = next((rows[i] for i in range(index, len(rows)) if rows[i].strip()), rows[index])
    threshold = _width(anchor[:len(anchor) - len(anchor.lstrip())])
    found = []
    for number in range(index - 1, -1, -1):
        if threshold == 0:
            break
        match = _SCOPE.match(rows[number])
        if match and _width(match.group(1)) < threshold:
            found.append(number + 1)
            threshold = _width(match.group(1))
    return found[::-1]


def context_block(text: str, line: int, language: str) -> str:
    rows = lines(text)
    if not rows:
        return ""
    line = min(max(line, 1), len(rows))
    shown = set(range(max(1, line - 3), min(len(rows), line + 2) + 1))
    if language == "python":
        shown.update(scopes(rows, line))
    out, previous = [], 0
    for number in sorted(shown):
        if number > previous + 1:
            out.append("    ⋮")
        mark = "█" if number == line else "│"
        out.append(f"{number:>5}{mark}{rows[number - 1][:LINE_CHARS]}")
        previous = number
    if previous < len(rows):
        out.append("    ⋮")
    # Keep the end, which holds the error window and the innermost scopes.
    out = out[-BLOCK_LINES:]
    while len(out) > 1 and sum(len(item) + 1 for item in out) - 1 > BLOCK_CHARS:
        out.pop(0)
    return "\n".join(out)[:BLOCK_CHARS]


def header(path: str, language: str, diagnostic: Diagnostic) -> str:
    version = f"Python {sys.version_info.major}.{sys.version_info.minor} parser"
    parser = version if language == "python" else f"{LABELS[language]}, {version}"
    where = f"line {diagnostic.line}" + (f", column {diagnostic.column}" if diagnostic.column else "")
    return (f"Syntax check failed for {path} ({parser}): {diagnostic.message} ({where}). "
            "The edit was not applied; the file is unchanged.")


def format_failure(path: str, language: str, text: str, diagnostic: Diagnostic) -> str:
    block = context_block(text, diagnostic.line, language)
    return header(path, language, diagnostic) + ("\n" + block if block else "")


def _summary(diagnostic: Diagnostic) -> str:
    where = f"line {diagnostic.line}" + (f", column {diagnostic.column}" if diagnostic.column else "")
    return f"{diagnostic.message} ({where})"


def guard(path, old, new, mode, notify):
    """Write guard: reject a parseable file becoming unparseable; warn otherwise.

    Returns None when nothing is checked, otherwise a check dict for the result.
    """
    if mode == "off":
        return None
    language = language_of(path)
    if language is None:
        return None
    base = {"type": "syntax", "path": path, "language": language}
    try:
        diagnostic = _parse(language, path, new)
    except NotChecked as exc:
        return {**base, "result": "not checked", "reason": str(exc)}
    if diagnostic is None:
        return {**base, "result": "ok"}
    error = {**base, "result": "error", "message": _summary(diagnostic), "line": diagnostic.line, "rejected": False}
    if old is None:
        return {**error, "note": "new file"}
    try:
        before = _parse(language, path, old)
    except NotChecked:
        return {**error, "note": "file could not be checked before this edit"}
    if before is not None:
        # Already broken, or a dialect or newer-version file the host parser rejects.
        return {**error, "note": "file already failed to parse before this edit"}
    if mode == "warn":
        return error
    # A regression. Any unknown mode fails safe as 'reject'.
    notify("syntax_check_failed", path=path, language=language, line=diagnostic.line, rejected=True)
    raise HarnessError(format_failure(path, language, new, diagnostic))


def parse_lint(values) -> tuple[tuple[str, str], ...]:
    """Parse repeatable GLOB=COMMAND values."""
    out = []
    for value in values or ():
        glob, sep, command = str(value).partition("=")
        if not sep or not glob.strip() or not command.strip():
            raise HarnessError(f"--lint-cmd must look like GLOB=COMMAND, for example '*.py=ruff check {{path}}': {value!r}")
        out.append((glob.strip(), command.strip()))
    return tuple(out)


def lint_command(template: str, path: str) -> str:
    quoted = shlex.quote(path if not path.startswith("-") else "./" + path)
    return template.replace("{path}", quoted) if "{path}" in template else f"{template} {quoted}"


def _head_tail(text: str, limit: int = LINT_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n…[{len(text) - limit} characters omitted]…\n"
    keep = (limit - len(marker)) // 2
    return text[:keep] + marker + text[len(text) - keep:]


def _changed_paths(result: dict) -> list[str]:
    paths = []
    if isinstance(result.get("path"), str):
        paths.append(result["path"])
    for entry in result.get("files") or ():
        if isinstance(entry, str):
            paths.append(entry)
        elif isinstance(entry, dict) and entry.get("op") not in ("delete", "deleted") and not entry.get("deleted"):
            target = entry.get("to") if isinstance(entry.get("to"), str) else entry.get("path")
            if isinstance(target, str):
                paths.append(target)
    normalized = []
    for path in paths:
        clean = PurePosixPath(path).as_posix()
        if clean not in normalized:
            normalized.append(clean)
    return normalized


def lint_hook(toolbox):
    """after_call hook: run configured lint commands in the sandbox for changed files."""
    def hook(name, arguments, result):
        commands = getattr(toolbox.policy, "lint_commands", ()) or ()
        if not commands or name not in EDIT_TOOLS or not isinstance(result, dict):
            return result
        if result.get("changed") is False or result.get("ok") is False:
            return result
        jobs = [(path, command) for path in _changed_paths(result)
                for glob, command in commands if fnmatch.fnmatchcase(path, glob)][:LINT_MAX_PER_CALL]
        if not jobs:
            return result
        checks = []
        for path, template in jobs:
            if toolbox.policy.shell_mode != "docker":
                checks.append({"type": "lint", "path": path, "status": "skipped", "reason": "shell is disabled"})
                continue
            command = lint_command(template, path)
            try:
                run = toolbox.shell(command, timeout=LINT_TIMEOUT)
            except Exception as exc:  # a lint failure must never look like a failed write
                checks.append({"type": "lint", "path": path, "command": command, "status": "skipped",
                               "reason": f"lint command not run: {exc}"})
                continue
            checks.append({"type": "lint", "path": path, "command": command, "exit_code": run.get("exit_code"),
                           "output": _head_tail(str(run.get("output", ""))),
                           **({"stopped": run["stopped"]} if run.get("stopped") else {})})
        existing = result.get("checks")
        result["checks"] = (list(existing) if isinstance(existing, list) else []) + checks
        return result
    return hook
