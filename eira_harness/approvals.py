"""Shell approval decisions: when a container command may run without a prompt.

Pure and stdlib-only. ``decide_shell`` is evaluated before every shell command.
Under ``--shell-approval sandboxed`` a command runs automatically only when the
mount plan reports ``protected: True``, no trust-handoff alert is pending and
the destructive-command heuristic finds nothing. Everything else asks, and a
headless run turns "ask" into a denial.

``destructive`` is a speed bump against accidents, not a security boundary:
quoting, variables, scripts and aliases can evade it. The protections are the
container, the mount plan and checkpoints.
"""
from __future__ import annotations

from dataclasses import dataclass
import shlex

from .security import HarnessError

SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash"})
WRAPPERS = frozenset({"sudo", "env", "nice", "nohup", "command", "exec", "time", "timeout", "xargs", "stdbuf"})
# Wrapper flags that consume the next token, so it is not taken as the command.
WRAPPER_FLAG_ARGS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "-R"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a", "--arg-file", "--delimiter",
              "--max-args", "--max-procs", "--max-lines", "--max-chars", "--eof", "--replace"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "time": {"-f", "-o", "--format", "--output"},
}
PUNCTUATION = ";&|()"
CRITICAL = frozenset({".", "./", "..", "../", "/", "/workspace", "/workspace/", "~", "~/", "*", "./*", "/*",
                      "/workspace/*", ".*", "$HOME", "${HOME}", "$PWD", "${PWD}", "/tmp", "/tmp/"})
MAX_DEPTH = 3


@dataclass(frozen=True)
class Decision:
    action: str  # "auto" or "ask"
    reason: str


def decide_shell(policy, plan, command: str, alerts) -> Decision:
    """The shell approval decision table, evaluated in order."""
    if getattr(policy, "read_only", False):
        raise HarnessError("Denied by read-only policy.")
    if getattr(policy, "shell_approval", "always") != "sandboxed":
        return Decision("ask", "approval mode always")
    if getattr(policy, "shell_mode", "disabled") != "docker":
        return Decision("ask", "sandbox protections unavailable")
    if not isinstance(plan, dict) or plan.get("protected") is not True:
        return Decision("ask", "sandbox protections unavailable")
    pending = _unique(alerts or [])
    if pending:
        shown = ", ".join(_short(path) for path in pending[:10])
        if len(pending) > 10:
            shown += f" and {len(pending) - 10} more"
        return Decision("ask", f"a previous command created protected config paths: {shown}; review before continuing")
    reason = destructive(command)
    if reason:
        return Decision("ask", reason)
    return Decision("auto", "sandboxed")


def journaled_alerts(events) -> list[str]:
    """Protected paths created by commands since the session's last approved shell command.

    ``events`` are journal rows ({kind, payload}) in order. This lets an alert
    outlive the Toolbox that raised it: chat builds a new Toolbox per prompt,
    and resumed sessions start with a fresh one.
    """
    pending: list[str] = []
    for event in events or []:
        kind = event.get("kind") if isinstance(event, dict) else None
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            continue
        if kind == "approval_decided" and payload.get("tool") == "shell" and payload.get("decision") == "approved":
            pending = []
        elif kind == "sandbox_protected_path_created":
            paths = payload.get("paths")
            if isinstance(paths, list):
                pending += [path for path in paths if isinstance(path, str) and path]
    return _unique(pending)


def destructive(command: str, depth: int = 0) -> str | None:
    """A short reason when the command matches a destructive pattern, else None."""
    if not isinstance(command, str) or depth > MAX_DEPTH:
        return None
    tokens = _tokens(command)
    if tokens is None:
        return None
    for element in _split(tokens, parens=False):
        words = _strip(element)
        if words and _name(words[0]) == "find":
            reason = _find(words)
            if reason:
                return reason
            continue
        # Parentheses open and close subshells; analyze each part on its own.
        for part in _split(element, parens=True):
            reason = _segment(_strip(part), depth)
            if reason:
                return reason
    return None


def _tokens(command: str) -> list[str] | None:
    text = command.replace("\\\r\n", " ").replace("\\\n", " ").replace("\r\n", ";").replace("\n", ";").replace("\r", ";")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=PUNCTUATION)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        # Unbalanced quotes: no speed bump. The container still applies.
        return None


def _punctuation(token: str) -> bool:
    return bool(token) and all(c in PUNCTUATION for c in token)


def _split(tokens: list[str], parens: bool) -> list[list[str]]:
    """Split on control operators (;, &&, ||, |, &), or with parens=True on ( and ).

    shlex groups adjacent punctuation, so ');' is one token: any punctuation
    token holding ;, & or | is a control operator.
    """
    parts, current = [], []
    for token in tokens:
        if _punctuation(token) and (parens or any(c in token for c in ";&|")):
            parts.append(current)
            current = []
        else:
            current.append(token)
    parts.append(current)
    return [part for part in parts if part]


def _name(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _is_assignment(word: str) -> bool:
    name, equals, _ = word.partition("=")
    return bool(equals) and bool(name) and (name[0].isalpha() or name[0] == "_") and \
        all(c.isalnum() or c == "_" for c in name)


def _strip(words: list[str]) -> list[str]:
    """Drop leading NAME=value assignments and wrapper commands with their flags."""
    index = 0
    while index < len(words):
        word = words[index]
        if _is_assignment(word):
            index += 1
            continue
        wrapper = _name(word)
        if wrapper not in WRAPPERS:
            break
        index += 1
        takes = WRAPPER_FLAG_ARGS.get(wrapper, set())
        while index < len(words) and words[index].startswith("-") and words[index] != "-":
            flag = words[index]
            index += 1
            if flag == "--":
                break
            if flag in takes:
                index += 1
        if wrapper == "timeout" and index < len(words):
            index += 1  # the duration
    return words[index:]


def _short(text: str) -> str:
    return text if len(text) <= 80 else text[:79] + "…"


def _unique(items) -> list[str]:
    seen, out = set(), []
    for item in items:
        if isinstance(item, str) and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _critical(operand: str) -> bool:
    if operand in CRITICAL:
        return True
    stripped = operand.rstrip("/")
    if stripped and stripped in CRITICAL:
        return True
    # '../..', './/.', '//' and the like name the workspace or an ancestor.
    return all(part in {"", ".", ".."} for part in operand.split("/"))


def _short_flags(word: str) -> str:
    return word[1:] if len(word) > 1 and word[0] == "-" and word[1] != "-" else ""


def _segment(words: list[str], depth: int) -> str | None:
    if not words:
        return None
    name = _name(words[0])
    args = words[1:]
    if name in SHELLS:
        script = _shell_script(args)
        return destructive(script, depth + 1) if script is not None else None
    if name == "eval" and args:
        return destructive(" ".join(args), depth + 1)
    if name == "rm":
        return _rm(args)
    if name == "find":
        return _find(words)
    if name == "git":
        return _git(args)
    if name == "shred":
        return "shred overwrites files"
    return None


def _shell_script(args: list[str]) -> str | None:
    index = 0
    while index < len(args):
        word = args[index]
        if word in {"-o", "+o"}:
            index += 2
            continue
        flags = _short_flags(word)
        if flags and flags.isalpha() and "c" in flags:
            rest = [arg for arg in args[index + 1:] if not (arg.startswith("-") and arg != "-")]
            return rest[0] if rest else None
        if not word.startswith(("-", "+")):
            return None  # a script file, not -c
        index += 1
    return None


def _rm(args: list[str]) -> str | None:
    recursive, operands, options = False, [], True
    for word in args:
        if options and word == "--":
            options = False
            continue
        if options and word.startswith("-") and word != "-":
            if word == "--recursive" or (_short_flags(word) and set(_short_flags(word)) & {"r", "R"}):
                recursive = True
            continue
        operands.append(word)
    if not recursive:
        return None
    for operand in operands:
        if _critical(operand) or "$(" in operand or "`" in operand or operand.startswith("$"):
            return f'recursive rm of "{_short(operand)}"'
    return None


GIT_OPTION_ARGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}


def _git(args: list[str]) -> str | None:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in GIT_OPTION_ARGS else 1
    if index >= len(args) or args[index] != "clean":
        return None
    force = ignored = dry_run = False
    for word in args[index + 1:]:
        if word == "--":
            break
        flags = _short_flags(word)
        force |= word == "--force" or "f" in flags
        ignored |= "x" in flags or "X" in flags
        dry_run |= word == "--dry-run" or "n" in flags
    if force and ignored and not dry_run:
        return "git clean of untracked and ignored files"
    return None


def _find(words: list[str]) -> str | None:
    args = words[1:]
    index = 0
    # Leading options: -H, -L, -P, -O<level>, -D <debug>.
    while index < len(args) and (args[index] in {"-H", "-L", "-P", "-D"} or args[index].startswith("-O")):
        index += 2 if args[index] == "-D" else 1
    paths = []
    while index < len(args) and not args[index].startswith(("-", "(", "!", ")")):
        paths.append(args[index])
        index += 1
    expression = args[index:]
    deletes = "-delete" in expression
    removes = any(expression[i] in {"-exec", "-execdir", "-ok", "-okdir"} and i + 1 < len(expression)
                  and _name(expression[i + 1]) == "rm" for i in range(len(expression)))
    if not (deletes or removes):
        return None
    action = "-delete" if deletes else "-exec rm"
    if not paths:
        return f"find {action} without a starting path"
    if any(_critical(path) or path.startswith("$") or "`" in path for path in paths):
        return f'find {action} in "{_short(paths[0])}"'
    return None
