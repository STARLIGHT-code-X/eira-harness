"""Mount plan for shell containers: hide secrets, make config read-only.

Before each approved Docker command Eira scans the workspace and builds a
mount plan. Secret paths that the file tools block are masked (an empty file
or an empty read-only tmpfs), and paths that host tools or later agent
sessions execute or trust (VCS metadata, agent instructions, IDE, hook, CI and
package-manager config) are bind-mounted read-only. Credential-bearing git
config files are replaced by a sanitized copy. After the command, a second
scan reports protected config paths that the command created or replaced.

Every scan problem fails closed before approval. See docs/SANDBOX.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePath
import re
import shutil
import time
from typing import Callable

from .security import HarnessError, protected_kind
from .text import split_lines

# Read-only inside the container and review-required for file tools: paths that
# host tools, IDEs, CI or later agent sessions execute or trust. Sources:
# Claude Code protected paths (https://code.claude.com/docs/en/permission-modes),
# Codex writable-root protections (.git, .agents, .codex; protocol/src/permissions.rs
# at 8f21b7f), and the trust-handoff research (Pillar "GitPwned", 2026-07-20;
# CSA research note, 2026-07-22). One path component, case-insensitive, any depth.
CONFIG_NAMES = frozenset({
    # VCS metadata (.git may be a directory or a gitdir file).
    ".git", ".hg", ".svn", ".bzr", ".gitmodules",
    # Agent config and instructions.
    ".codex", ".agents", ".claude", ".gemini", ".cursor", ".mcp.json", ".claude.json",
    "eira.md", "agents.md", "agents.override.md", "claude.md", "gemini.md",
    # IDE and devcontainer config (tasks, launch configs and extensions run code).
    ".vscode", ".idea", ".zed", ".devcontainer", ".devcontainer.json",
    # Hook managers and automation.
    ".husky", ".githooks", ".pre-commit-config.yaml", "lefthook.yml", "lefthook.yaml",
    ".lefthook.yml", ".lefthook.yaml", ".envrc",
    # CI definitions (two-component entries are in CONFIG_PAIRS).
    ".gitlab-ci.yml", ".circleci", ".buildkite", "azure-pipelines.yml",
    "bitbucket-pipelines.yml", ".travis.yml", "jenkinsfile",
    # Package-manager and tool hooks that run on install, build or search.
    ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs", ".pnp.cjs", ".pnp.loader.mjs", "bunfig.toml",
    ".bunfig.toml", ".cargo", ".mvn", "gradle-wrapper.properties", "maven-wrapper.properties",
    ".bazelrc", ".bazelversion", ".bazeliskrc", ".ripgreprc", "pyrightconfig.json",
    # Shell rc files not already blocked as secrets by Workspace.BLOCKED.
    ".bash_login", ".bash_aliases", ".bash_logout", ".zprofile", ".zshenv", ".zlogin", ".zlogout",
})
# Entries written with a slash: the last two path components must equal them.
CONFIG_PAIRS = frozenset({(".github", "workflows"), (".github", "actions"),
                          (".yarn", "plugins"), (".yarn", "releases")})
# A symlink in place of these parents would let a command redirect the pair.
PAIR_PARENTS = frozenset(parent for parent, _ in CONFIG_PAIRS)
VCS_DIRS = frozenset({".git", ".hg", ".svn", ".bzr"})
# Not descended into: generated trees whose size would dominate every scan.
HEAVY_DIRS = frozenset({"node_modules", ".venv", "venv", "__pycache__", ".tox", ".nox",
                        ".mypy_cache", ".pytest_cache", ".ruff_cache"})

SCAN_MAX_ENTRIES = 200_000
SCAN_SECONDS = 5.0
SCAN_MAX_DEPTH = 64
MAX_MOUNTS = 512
MAX_GIT_CONFIGS = 64
MAX_GIT_CONFIG_BYTES = 1_000_000
CONTAINER_ENV = (("HOME", "/tmp"), ("LANG", "C.UTF-8"), ("TERM", "dumb"), ("NO_COLOR", "1"),
                 ("PAGER", "cat"), ("GIT_PAGER", "cat"), ("GIT_OPTIONAL_LOCKS", "0"),
                 ("GIT_CONFIG_NOSYSTEM", "1"))
# Credential locations that Workspace.path also protects when they fall inside a workspace.
CREDENTIAL_VARIABLES = ("XDG_CONFIG_HOME", "CLOUDSDK_CONFIG", "GH_CONFIG_DIR",
                        "AWS_SHARED_CREDENTIALS_FILE", "GOOGLE_APPLICATION_CREDENTIALS", "KUBECONFIG")


def classify(parent: str, name: str) -> str | None:
    """'secret', 'config' or None for one entry, given its parent's name."""
    if protected_kind(name) == "secret":
        return "secret"
    lower = name.lower()
    if lower in CONFIG_NAMES or (parent.lower(), lower) in CONFIG_PAIRS:
        return "config"
    return None


def requires_review(path) -> bool:
    """True when a file-tool write to this workspace-relative path needs a human diff review."""
    if not isinstance(path, str):
        return True
    parts = [part.lower() for part in PurePath(path).parts]
    return any(part in CONFIG_NAMES or (index and (parts[index - 1], part) in CONFIG_PAIRS)
               for index, part in enumerate(parts))


@dataclass
class Scan:
    secret_files: list[str] = field(default_factory=list)
    secret_dirs: list[str] = field(default_factory=list)
    config_paths: list[str] = field(default_factory=list)
    config_symlinks: list[str] = field(default_factory=list)
    entries: int = 0
    seconds: float = 0.0
    # Harness-side detail: (st_dev, st_ino) per config path and symlink, and
    # the config paths that are VCS directories (git config is sanitized there).
    identities: dict = field(default_factory=dict)
    git_dirs: list[str] = field(default_factory=list)


def _limit_error(entries: int) -> HarnessError:
    return HarnessError(f"Eira could not verify protected paths in this workspace ({entries:,} entries scanned). "
                        "Use a smaller workspace for shell commands.")


def scan(root, config_only: bool = False) -> Scan:
    """Walk the workspace without following symlinks and classify protected paths.

    Exceeding the entry, time or depth limit raises HarnessError. With
    config_only, secrets are not collected (the post-run check needs only
    config paths).
    """
    root = Path(root)
    started = time.monotonic()
    deadline = started + SCAN_SECONDS
    result = Scan()
    # (directory, relative prefix, depth, inside a read-only config directory)
    pending = [(root, "", 0, False)]
    while pending:
        directory, prefix, depth, inside = pending.pop()
        if depth > SCAN_MAX_DEPTH:
            raise _limit_error(result.entries)
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            # The container runs as the same user without capabilities, so a
            # directory this process cannot traverse is closed to it too.
            if not os.access(directory, os.X_OK):
                continue
            raise HarnessError(f"Eira could not read {json.dumps(prefix or '.')} while checking protected paths "
                               f"({type(exc).__name__}).") from exc
        parent = directory.name if prefix else ""
        children = []
        for entry in entries:
            result.entries += 1
            if result.entries > SCAN_MAX_ENTRIES or time.monotonic() > deadline:
                raise _limit_error(result.entries)
            name, lower = entry.name, entry.name.lower()
            relative = prefix + name
            if not prefix and lower == ".eira":
                continue  # the workspace's own state already has a tmpfs
            kind = classify(parent, name)
            if inside and kind == "config":
                kind = None  # already read-only through the enclosing mount
            try:
                is_link = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_link:
                # A masked name that is a link is skipped: its target is
                # classified on its own, and an absolute link resolves to
                # container paths. A linked config path could redirect host
                # tools, so it is recorded and the plan fails closed.
                if kind == "config" or (not inside and lower in PAIR_PARENTS):
                    result.config_symlinks.append(relative)
                    result.identities[relative] = _identity(entry)
                continue
            if kind == "secret":
                if not config_only:
                    (result.secret_dirs if is_dir else result.secret_files).append(relative)
                continue
            if kind == "config":
                result.config_paths.append(relative)
                result.identities[relative] = _identity(entry)
                if is_dir and lower in VCS_DIRS:
                    result.git_dirs.append(relative)
                elif is_dir and not config_only:
                    # Secrets inside a read-only config directory are still masked.
                    children.append((Path(entry.path), relative + "/", depth + 1, True))
                continue
            if lower == ".eira":
                # Another workspace's state: blocked for file tools, so masked.
                if is_dir and not config_only:
                    result.secret_dirs.append(relative)
                continue
            if is_dir and lower not in HEAVY_DIRS:
                children.append((Path(entry.path), relative + "/", depth + 1, inside))
        # Reverse so the stack visits children in sorted order.
        pending.extend(reversed(children))
    result.seconds = round(time.monotonic() - started, 4)
    return result


def _identity(entry) -> tuple[int, int] | None:
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    return info.st_dev, info.st_ino


_UNSAFE = re.compile(r'[,:"\x00-\x1f\x7f]')


def _check_expressible(relative: str) -> None:
    if _UNSAFE.search(relative):
        raise HarnessError(f"Protected path {json.dumps(relative)} cannot be expressed safely in a Docker mount; "
                           "rename it before running shell commands.")


def _credential_paths(root: Path) -> list[tuple[str, bool]]:
    """Configured credential locations inside the workspace, as (relative path, is directory)."""
    found = []
    for variable in CREDENTIAL_VARIABLES:
        for item in filter(None, os.environ.get(variable, "").split(os.pathsep)):
            location = Path(item).expanduser()
            if not location.is_absolute() or location.is_symlink() or not location.exists():
                continue
            location = location.resolve()
            if location != root and location.is_relative_to(root):
                found.append((location.relative_to(root).as_posix(), location.is_dir()))
    return found


# ---- git config sanitizer -------------------------------------------------

_HEADER = re.compile(r'\s*\[((?:[^\]"\\]|"(?:[^"\\]|\\.)*")*)\]')
_KEY = re.compile(r"\s*([A-Za-z][A-Za-z0-9-]*)\s*(?:=|[#;]|$)")
_URL_USERINFO = re.compile(r'([A-Za-z][A-Za-z0-9+.-]*://)([^/\s@"]*)@')
_CREDENTIAL_SECTION = re.compile(r'credential(?:$|[\s."])', re.IGNORECASE)
DROP_KEYS = frozenset({"extraheader", "helper", "password", "token"})


def _strip_userinfo(text: str) -> str:
    def replace(match):
        scheme, userinfo = match.group(1), match.group(2)
        if ":" in userinfo or scheme.lower() in {"http://", "https://", "ftp://", "ftps://"}:
            return scheme
        return match.group(0)
    return _URL_USERINFO.sub(replace, text)


def _continues(line: str) -> bool:
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def sanitize_git_config(text: str, redact: Callable[[str], str] | None = None) -> str:
    """Remove credentials from git config text; lines without credentials stay byte-identical.

    Strips URL userinfo that carries a password (or any http(s)/ftp(s)
    userinfo), drops extraheader, helper, password and token keys and every
    [credential...] section, and finally applies the redactor.
    """
    out, in_credential, drop_next, keep_next = [], False, False, False
    for line, ending in split_lines(text):
        if drop_next:
            drop_next = _continues(line)
            continue
        if keep_next:
            keep_next = _continues(line)
            out.append(_strip_userinfo(line) + ending)
            continue
        header = _HEADER.match(line)
        if header:
            in_credential = bool(_CREDENTIAL_SECTION.match(header.group(1).strip()))
            if in_credential:
                drop_next = _continues(line)
                continue
            rest = line[header.end():]
            key = _KEY.match(rest)
            if key and key.group(1).lower() in DROP_KEYS:
                out.append(line[:header.end()] + ending)
                drop_next = _continues(line)
                continue
            out.append(_strip_userinfo(line) + ending)
            keep_next = _continues(line)
            continue
        if in_credential:
            drop_next = _continues(line)
            continue
        key = _KEY.match(line)
        if key and key.group(1).lower() in DROP_KEYS:
            drop_next = _continues(line)
            continue
        out.append(_strip_userinfo(line) + ending)
        keep_next = _continues(line)
    result = "".join(out)
    return redact(result) if redact is not None else result


def _git_config_files(root: Path, git_dir: str) -> list[str]:
    """Relative paths of a .git directory's config files: top level and modules/**."""
    files, visited = [], 0
    pending = [(root / git_dir, git_dir, 0)]
    while pending:
        directory, relative, depth = pending.pop()
        for name in ("config", "config.worktree"):
            candidate = directory / name
            if candidate.is_symlink():
                raise HarnessError(f"Protected path {json.dumps(relative + '/' + name)} is a symlink; "
                                   "the sandbox cannot protect it.")
            if candidate.is_file():
                files.append(f"{relative}/{name}")
        modules = [(directory / "modules", relative + "/modules", depth + 1)]
        while modules:
            folder, folder_relative, folder_depth = modules.pop()
            if folder.is_symlink() or not folder.is_dir():
                continue
            if folder_depth > 32:
                raise HarnessError("Eira could not verify the git config files in this workspace. "
                                   "Use a smaller workspace for shell commands.")
            with os.scandir(folder) as iterator:
                children = sorted((entry for entry in iterator if entry.is_dir(follow_symlinks=False)),
                                  key=lambda entry: entry.name, reverse=True)
            for child in children:
                visited += 1
                if visited > 4096 or len(files) > MAX_GIT_CONFIGS:
                    raise HarnessError("Eira could not verify the git config files in this workspace. "
                                       "Use a smaller workspace for shell commands.")
                child_path, child_relative = Path(child.path), f"{folder_relative}/{child.name}"
                if (child_path / "config").exists() or (child_path / "HEAD").exists():
                    pending.append((child_path, child_relative, folder_depth + 1))
                else:
                    # Module names may contain '/', so intermediate folders are walked.
                    modules.append((child_path, child_relative, folder_depth + 1))
    if len(files) > MAX_GIT_CONFIGS:
        raise HarnessError("Eira could not verify the git config files in this workspace. "
                           "Use a smaller workspace for shell commands.")
    return sorted(files)


def _sanitized_configs(root: Path, git_dirs: list[str], redact) -> list[tuple[str, bytes]]:
    sanitized = []
    for git_dir in git_dirs:
        for relative in _git_config_files(root, git_dir):
            path = root / relative
            if path.stat().st_size > MAX_GIT_CONFIG_BYTES:
                raise HarnessError(f"Git config {json.dumps(relative)} is too large to check for credentials.")
            data = path.read_bytes()
            text = data.decode("utf-8", errors="surrogateescape")
            clean = sanitize_git_config(text, redact)
            if clean != text:
                sanitized.append((relative, clean.encode("utf-8", errors="surrogateescape")))
    return sanitized


# ---- plan, argv and post-run check ----------------------------------------

def build(root, redact: Callable[[str], str] | None = None) -> dict:
    """Scan and build the protective mount plan; every problem raises HarnessError."""
    root = Path(root)
    found = scan(root)
    if found.config_symlinks:
        raise HarnessError(f"Protected path {json.dumps(found.config_symlinks[0])} is a symlink; "
                           "the sandbox cannot protect it.")
    try:
        git_configs = _sanitized_configs(root, found.git_dirs, redact)
    except OSError as exc:
        raise HarnessError(f"Eira could not check git config files for credentials ({type(exc).__name__}).") from exc
    secrets = [("mask_file", path) for path in found.secret_files] + [("mask_dir", path) for path in found.secret_dirs]
    known = {path for _, path in secrets}
    for relative, is_dir in _credential_paths(root):
        if relative not in known:
            secrets.append(("mask_dir" if is_dir else "mask_file", relative))
            known.add(relative)
    masked_dirs = [path for kind, path in secrets if kind == "mask_dir"]

    def hidden(path):
        # A mount below a masked directory would need a mountpoint in an empty tmpfs.
        return any(path != folder and path.startswith(folder + "/") for folder in masked_dirs)
    mounts = [item for item in secrets if not hidden(item[1])]
    mounts += [("readonly", path) for path in found.config_paths if path not in known and not hidden(path)]
    mounts += [("git_config", relative, index) for index, (relative, _) in enumerate(git_configs, 1)
               if not hidden(relative)]
    for mount in mounts:
        _check_expressible(mount[1])
    mounts.sort(key=lambda mount: mount[1])
    if len({mount[1] for mount in mounts}) != len(mounts):
        raise HarnessError("The sandbox mount plan has overlapping protected paths.")
    if len(mounts) > MAX_MOUNTS:
        raise HarnessError(f"This workspace has {len(mounts):,} protected paths; the sandbox supports at most "
                           f"{MAX_MOUNTS}. Use a smaller workspace for shell commands.")
    masked = sum(1 for mount in mounts if mount[0] in {"mask_file", "mask_dir"})
    read_only = sum(1 for mount in mounts if mount[0] == "readonly")
    summary = (f"Sandbox: no network, read-only system, {_count(masked, 'secret path')} hidden, "
               f"{_count(read_only, 'config path')} read-only")
    if git_configs:
        summary += f", {_count(len(git_configs), 'git config')} sanitized"
    return {"mounts": mounts, "scan": found, "git_configs": git_configs, "summary": summary,
            "masked": masked, "read_only": read_only}


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def signature(plan: dict) -> list:
    """What the user approved: the protective mounts, without file contents."""
    return [tuple(mount[:2]) for mount in plan["mounts"]]


def copies_dir(root, container: str) -> Path:
    return Path(root) / ".eira" / "sandbox" / container


def prepare(root, container: str, plan: dict) -> None:
    """Write the sanitized git config copies (mode 0600) that the plan mounts."""
    if not plan["git_configs"]:
        return
    directory = copies_dir(root, container)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    for index, (_, data) in enumerate(plan["git_configs"], 1):
        descriptor = os.open(directory / f"git-config-{index}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)


def cleanup(root, container: str) -> None:
    shutil.rmtree(copies_dir(root, container), ignore_errors=True)


def protective_args(root, container: str, plan: dict) -> list[str]:
    args = []
    for mount in plan["mounts"]:
        kind, relative = mount[0], mount[1]
        destination = f"/workspace/{relative}"
        if kind == "mask_file":
            args += ["--mount", f"type=bind,src=/dev/null,dst={destination},readonly"]
        elif kind == "mask_dir":
            args += ["--tmpfs", f"{destination}:ro,size=4k,mode=0500"]
        elif kind == "readonly":
            args += ["--mount", f"type=bind,src={Path(root) / relative},dst={destination},readonly"]
        elif kind == "git_config":
            source = copies_dir(root, container) / f"git-config-{mount[2]}"
            args += ["--mount", f"type=bind,src={source},dst={destination},readonly"]
    return args


def docker_argv(docker, container, image, root, uid, gid, command, plan) -> list[str]:
    env = [arg for name, value in CONTAINER_ENV for arg in ("-e", f"{name}={value}")]
    return [docker, "run", "--pull=never", "--name", container, "--label", "eira.managed=1",
            "--log-driver=none", "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=512m", "--cpus=1",
            "--user", f"{uid}:{gid}",
            "--mount", f"type=bind,src={root},dst=/workspace",
            "--tmpfs", "/workspace/.eira:rw,size=1m,mode=0700",
            *protective_args(root, container, plan),
            "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--workdir", "/workspace", *env,
            "--entrypoint", "/bin/sh", image, "-c", command]


def created_paths(root, before: Scan) -> list[str]:
    """Config paths present after a command that were absent, or a different file, before it."""
    after = scan(root, config_only=True)
    return sorted(path for path in after.config_paths + after.config_symlinks
                  if path not in before.identities or before.identities[path] != after.identities.get(path))


__all__ = ["CONFIG_NAMES", "CONFIG_PAIRS", "Scan", "build", "classify", "cleanup", "created_paths",
           "docker_argv", "prepare", "requires_review", "sanitize_git_config", "scan", "signature"]
