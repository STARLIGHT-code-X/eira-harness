"""Workspace navigation: a bounded walk honoring .gitignore, paging, and line search.

Every listed or searched path passes Workspace.path, so state, VCS, credential,
symlinked and hard-linked paths stay invisible whatever the flags. .gitignore
only ever hides files; it never reveals a blocked one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time

from . import search_worker
from .security import HarnessError, Workspace
from .text import split_lines

MAX_TEXT_FILE = 5_000_000  # Same as tools.MAX_TEXT_FILE (asserted in tests).
WALK_DEPTH = 32
WALK_ENTRIES = 200_000
WALK_SECONDS = 5
LITERAL_SECONDS = 5
REGEX_TIMEOUT = 10
REGEX_MAX_LINE_CHARS = 10_000
WORKER_OUTPUT_LIMIT = 8 * 1024 * 1024
WORKER = Path(__file__).resolve().with_name("search_worker.py")
GITIGNORE_BYTES = 100_000
GITIGNORE_FILES = 200
HEAVY_DIRS = frozenset({"node_modules", "__pycache__", "venv", ".venv", ".tox", ".nox",
                        ".mypy_cache", ".pytest_cache", ".ruff_cache"})


# --- .gitignore (a documented subset of gitignore(5)) ---------------------

@dataclass(frozen=True)
class Rule:
    regex: re.Pattern
    negate: bool
    dir_only: bool
    anchored: bool


def _escaped(text: str, index: int) -> bool:
    slashes = 0
    while index - 1 - slashes >= 0 and text[index - 1 - slashes] == "\\":
        slashes += 1
    return slashes % 2 == 1


def _class(pattern: str, start: int):
    """Translate a [...] class at start; return (regex, next index) or None if unclosed."""
    index = start + 1
    negate = index < len(pattern) and pattern[index] in "!^"
    if negate:
        index += 1
    body, first = [], True
    while index < len(pattern):
        char = pattern[index]
        if char == "]" and not first:
            inner = "".join(body)
            return (f"[^/{inner}]" if negate else f"(?!/)[{inner}]"), index + 1
        if char == "\\":
            if index + 1 >= len(pattern):
                return None
            body.append(re.escape(pattern[index + 1]))
            index += 2
        else:
            body.append(char if char == "-" else re.escape(char))
            index += 1
        first = False
    return None


def _translate(pattern: str) -> str | None:
    """gitignore glob to regex; None when the pattern can never match."""
    out, index, size = [], 0, len(pattern)
    while index < size:
        char = pattern[index]
        if char == "\\":
            if index + 1 >= size:
                return None  # A trailing backslash never matches.
            out.append(re.escape(pattern[index + 1]))
            index += 2
        elif char == "*":
            end = index
            while end < size and pattern[end] == "*":
                end += 1
            whole = end - index >= 2 and (index == 0 or pattern[index - 1] == "/") and (end == size or pattern[end] == "/")
            if not whole:
                out.append("[^/]*")  # Other consecutive asterisks are regular asterisks.
            elif end == size:
                out.append(".*")  # 'abc/**' matches everything inside; a lone '**' matches all.
            else:
                out.append("(?:.*/)?")  # Leading '**/' or inner '/**/': zero or more directories.
                end += 1
            index = end
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[":
            translated = _class(pattern, index)
            if translated is None:
                out.append(re.escape("["))
                index += 1
            else:
                out.append(translated[0])
                index = translated[1]
        else:
            out.append(re.escape(char))
            index += 1
    return "".join(out)


def parse_rule(line: str) -> Rule | None:
    if not line or line.startswith("#"):
        return None
    negate = line.startswith("!")
    if negate:
        line = line[1:]
    while line.endswith(" ") and not _escaped(line, len(line) - 1):
        line = line[:-1]
    dir_only = line.endswith("/") and not _escaped(line, len(line) - 1)
    if dir_only:
        line = line[:-1]
    if not line:
        return None
    anchored = "/" in line
    if line.startswith("/"):
        line = line[1:]
    regex = _translate(line)
    if regex is None or not line:
        return None
    try:
        compiled = re.compile(regex, re.DOTALL)
    except re.error:
        return None  # An invalid class (such as [z-a]) never matches.
    return Rule(compiled, negate, dir_only, anchored)


def parse_gitignore(text: str) -> tuple[Rule, ...]:
    return tuple(rule for line, _ in split_lines(text) if (rule := parse_rule(line)) is not None)


def is_ignored(relative: str, is_dir: bool, chain) -> bool:
    """Deeper files override shallower ones; within a file the last match wins."""
    for base, rules in reversed(chain):
        local = relative if not base else relative[len(base) + 1:]
        name = local.rsplit("/", 1)[-1]
        for rule in reversed(rules):
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.fullmatch(local if rule.anchored else name):
                return not rule.negate
    return False


# --- Walk -----------------------------------------------------------------

@dataclass
class Walk:
    files: list[tuple[str, Path]] = field(default_factory=list)
    skipped: dict = field(default_factory=lambda: {"ignored": 0, "heavy_dirs": 0, "blocked": 0})
    stopped: str | None = None  # 'depth', 'entries' or 'time' when the walk is incomplete


def _join(directory: str, name: str) -> str:
    return name if directory in ("", ".") else f"{directory}/{name}"


def walk(workspace: Workspace, start: str = ".", *, include_ignored: bool = False, glob=None) -> Walk:
    """Every visible regular file under start, sorted by relative path."""
    from .tools import glob_match  # tools imports this module

    glob = glob or None  # An empty glob filters nothing, as before.
    if glob is not None:
        glob_match("x", glob)  # Fail fast on a malformed pattern.
    root = workspace.path(start)
    if not root.is_dir():
        raise HarnessError("Path must be a directory.")
    base = root.relative_to(workspace.root).as_posix()
    base = "" if base == "." else base
    result = Walk()
    protected = workspace.protected_locations()  # One snapshot per walk; every entry is still checked.
    budget = {"gitignores": 0}

    def rules_for(directory: str) -> tuple:
        if budget["gitignores"] >= GITIGNORE_FILES:
            result.skipped["gitignore_unread"] = result.skipped.get("gitignore_unread", 0) + 1
            return ()
        budget["gitignores"] += 1
        try:
            return parse_gitignore(workspace.read(_join(directory, ".gitignore"), GITIGNORE_BYTES))
        except (HarnessError, UnicodeError, OSError):
            result.skipped["gitignore_unread"] = result.skipped.get("gitignore_unread", 0) + 1
            return ()

    chain: tuple = ()
    if not include_ignored and base:
        parts = base.split("/")
        for depth in range(len(parts)):
            ancestor = "/".join(parts[:depth])
            if os.path.isfile(workspace.root / ancestor / ".gitignore") and not os.path.islink(workspace.root / ancestor / ".gitignore"):
                chain += ((ancestor, rules_for(ancestor)),)
    pending = [(root, base, 0, chain)]
    scanned, deadline = 0, time.monotonic() + WALK_SECONDS
    while pending:
        directory, relative_dir, depth, chain = pending.pop()
        if depth >= WALK_DEPTH:
            result.stopped = result.stopped or "depth"
            continue
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            result.skipped["blocked"] += 1
            continue
        if not include_ignored and any(entry.name == ".gitignore" and entry.is_file(follow_symlinks=False)
                                       for entry in entries):
            chain = chain + ((relative_dir, rules_for(relative_dir)),)
        children = []
        for entry in entries:
            scanned += 1
            if scanned > WALK_ENTRIES or time.monotonic() > deadline:
                result.stopped = "entries" if scanned > WALK_ENTRIES else "time"
                pending.clear()
                break
            relative = _join(relative_dir, entry.name)
            if entry.is_symlink():
                result.skipped["blocked"] += 1
                continue
            try:
                workspace.path(relative, protected)
            except HarnessError:
                result.skipped["blocked"] += 1
                continue
            if entry.is_dir(follow_symlinks=False):
                if not include_ignored and entry.name in HEAVY_DIRS:
                    result.skipped["heavy_dirs"] += 1
                elif not include_ignored and is_ignored(relative, True, chain):
                    result.skipped["ignored"] += 1  # Pruned: children cannot be re-included, as in git.
                else:
                    children.append((Path(entry.path), relative, depth + 1, chain))
            elif entry.is_file(follow_symlinks=False):
                if glob is not None and not glob_match(relative, glob):
                    continue
                if not include_ignored and is_ignored(relative, False, chain):
                    result.skipped["ignored"] += 1
                    continue
                result.files.append((relative, Path(entry.path)))
        if result.stopped in ("entries", "time"):
            break
        pending.extend(reversed(children))
    result.files.sort(key=lambda item: item[0])
    return result


def _stopped_note(stopped: str) -> str:
    return {"depth": f"Directories deeper than {WALK_DEPTH} levels were not listed.",
            "entries": f"The walk stopped after {WALK_ENTRIES:,} entries; narrow path or glob.",
            "time": f"The walk stopped after {WALK_SECONDS} s; narrow path or glob."}[stopped]


def list_page(found: Walk, offset: int = 0, limit: int = 500) -> dict:
    paths = [relative for relative, _ in found.files]
    page = paths[offset:offset + limit]
    more = offset + len(page) < len(paths)
    result = {"files": page, "truncated": more or found.stopped is not None,
              "next_offset": offset + len(page) if more else None, "skipped": dict(found.skipped)}
    if found.stopped:
        result["note"] = _stopped_note(found.stopped)
    return result


# --- Search ---------------------------------------------------------------

def _run_worker(request: dict) -> dict:
    """Run the regex worker in an isolated interpreter with a hard timeout."""
    payload = memoryview(json.dumps(request, ensure_ascii=True).encode("ascii"))
    process = subprocess.Popen([sys.executable, "-I", str(WORKER)], cwd="/", env={},
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    output, reason = bytearray(), None
    deadline = time.monotonic() + REGEX_TIMEOUT
    try:
        os.set_blocking(process.stdin.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            finished = False
            while not finished:
                left = deadline - time.monotonic()
                if left <= 0:
                    reason = "timeout"
                    break
                for key, _ in selector.select(left):
                    if key.fileobj is process.stdin:
                        try:
                            written = os.write(process.stdin.fileno(), payload[:65536])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            written = len(payload)
                        payload = payload[written:]
                        if not payload:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        data = os.read(process.stdout.fileno(), 65536)
                        if not data:
                            finished = True
                            break
                        output += data
                        if len(output) > WORKER_OUTPUT_LIMIT:
                            reason = "output_limit"
                            finished = True
                            break
            if reason is None:
                try:
                    process.wait(timeout=max(.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    reason = "timeout"
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError:
                pass
    if reason == "timeout":
        raise HarnessError(f"Regex search timed out after {REGEX_TIMEOUT:g} s; simplify the pattern or narrow path/glob.")
    if reason == "output_limit":
        raise HarnessError("Regex search output exceeded 8 MiB; lower max_results or narrow path/glob.")
    if process.returncode != 0:
        raise HarnessError("Regex search failed.")
    try:
        return json.loads(output.decode("ascii"))
    except (UnicodeError, ValueError) as exc:
        raise HarnessError("Regex search failed.") from exc


def search(workspace: Workspace, query: str, path: str = ".", glob=None, ignore_case: bool = False,
           regex: bool = False, context: int = 0, max_results: int = 100, output: str = "matches",
           ignored: bool = False) -> dict:
    if not query:
        raise HarnessError("Search query cannot be empty.")
    if output not in ("matches", "files", "count"):
        raise HarnessError("Unsupported value for output.")
    flags = re.IGNORECASE if ignore_case else 0
    if regex:
        if len(query) > 500:
            raise HarnessError("Regular expressions are limited to 500 characters.")
        try:
            re.compile(query, flags)  # Fail fast, before any walk or subprocess.
        except (re.error, OverflowError, RecursionError) as exc:
            raise HarnessError(f"Invalid regular expression: {exc}.") from exc
    found = walk(workspace, path, include_ignored=ignored, glob=glob)
    names = [relative for relative, _ in found.files]
    paths = [str(absolute) for _, absolute in found.files]
    if not paths:
        raw = search_worker.scan([], lambda line: -1)
    elif regex:
        raw = _run_worker({"pattern": query, "flags": int(flags), "files": paths, "max_results": max_results,
                           "context": context, "max_line_chars": REGEX_MAX_LINE_CHARS, "output": output,
                           "max_bytes": MAX_TEXT_FILE})
    else:
        needle = query.casefold() if ignore_case else query
        deadline = time.monotonic() + LITERAL_SECONDS
        raw = search_worker.scan(paths, (lambda line: line.casefold().find(needle)) if ignore_case else
                                 (lambda line: line.find(needle)), output, max_results, context,
                                 MAX_TEXT_FILE, split=split_lines, expired=lambda: time.monotonic() > deadline)
    skipped = {**raw["skipped"], **found.skipped}
    truncated = bool(raw["truncated"]) or found.stopped is not None
    if output == "matches":
        matches = []
        for hit in raw["hits"]:
            item = {"path": names[hit["file"]], "line": hit["line"], "column": hit["column"], "text": hit["text"]}
            if "before" in hit:
                item.update(before=hit["before"], after=hit["after"])
            matches.append(item)
        result = {"matches": matches, "files_searched": raw["files_searched"], "truncated": truncated,
                  "skipped": skipped}
    elif output == "files":
        result = {"files": [{"path": names[index], "count": count} for index, count in raw["counts"]],
                  "total_matches": raw["total_matches"], "files_searched": raw["files_searched"],
                  "truncated": truncated, "skipped": skipped}
    else:
        result = {"total_matches": raw["total_matches"], "files_with_matches": len(raw["counts"]),
                  "files_searched": raw["files_searched"], "truncated": truncated, "skipped": skipped}
    unread = sum(raw["skipped"].values())
    if unread:
        result["skipped_files"] = unread
        result["note"] = "Some files were not searched: see skipped (binary, too_large over 5 MB, non_utf8, unreadable)."
    if found.stopped:
        result["note"] = (result.get("note", "") + " " + _stopped_note(found.stopped)).strip()
    return result
