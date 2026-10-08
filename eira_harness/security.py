"""Boundary checks and terminal/log hygiene. These are not an OS sandbox."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile


class HarnessError(Exception):
    """A user-actionable error, safe to render without a traceback."""


def clean_terminal(text: str) -> str:
    # Single pass; malformed escape strings cannot cause regex backtracking.
    if not isinstance(text, str):
        raise HarnessError("Terminal output must be text.")
    out, index = [], 0
    while index < len(text):
        char = text[index]
        if char == "\x1b" and index + 1 < len(text):
            kind = text[index + 1]
            if kind == "]":
                index += 2
                while index < len(text):
                    if text[index] == "\x07":
                        index += 1
                        break
                    if text[index:index+2] == "\x1b\\":
                        index += 2
                        break
                    index += 1
                continue
            if kind == "[":
                index += 2
                while index < len(text) and not "@" <= text[index] <= "~":
                    index += 1
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(c for c in out if c in "\n\t" or
                   (ord(c) >= 32 and not 127 <= ord(c) <= 159
                    and c not in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))


def approval_text(text: str) -> str:
    # Preserve every byte-equivalent character visibly. Unlike ordinary output,
    # approval material must never delete hidden executable content.
    return "\n".join(json.dumps(line, ensure_ascii=True) for line in text.split("\n"))


def bounded_json_loads(text, max_depth=32):
    """Reject excessive nesting before the recursive stdlib decoder runs."""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    if not isinstance(text, str) or len(text) > 2_000_000:
        raise HarnessError("JSON input is invalid or too large.")
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                raise HarnessError("JSON nesting limit exceeded.")
        elif char in "]}":
            depth -= 1
    def reject(value):
        raise ValueError("Nonfinite JSON number")
    try:
        return json.loads(text, parse_constant=reject)
    except (ValueError, RecursionError) as exc:
        raise HarnessError("Invalid JSON input.") from exc


class Redactor:
    def __init__(self, extra: tuple[str, ...] = ()):
        self.secrets = sorted({value for name, value in os.environ.items()
                               if any(word in name.upper() for word in
                                      ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
                               and len(value) >= 8} | {x for x in extra if len(x) >= 8},
                              key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}", "[REDACTED]", text)
        return text


def redact_tree(value, redact, depth=0):
    """Redact every string in a JSON-like value before it is encoded.

    Redacting encoded text misses secrets whose quotes or backslashes were
    escaped, so callers redact first and encode afterwards.
    """
    if depth > 64:
        raise HarnessError("Data exceeds the nesting limit.")
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_tree(item, redact, depth + 1) for item in value]
    if isinstance(value, dict):
        return {redact(str(key)): redact_tree(item, redact, depth + 1) for key, item in value.items()}
    return value


class Workspace:
    BLOCKED = {
        ".eira", ".git", ".hg", ".svn", ".bzr", ".ssh", ".aws", ".gnupg",
        ".kube", ".codex", ".netrc", "_netrc", ".npmrc", ".pypirc", ".docker",
        ".git-credentials", ".azure", ".gcloud", ".password-store",
        ".config", ".gitconfig", ".bashrc", ".bash_profile", ".profile", ".zshrc",
        "application_default_credentials.json", "service_account.json",
    }

    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise HarnessError("Workspace must be a directory.")

    def path(self, relative: str, protected: list[Path] | None = None) -> Path:
        """Validate a workspace-relative path; protected is a protected_locations() snapshot."""
        if (not isinstance(relative, str) or not relative or len(relative) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in relative) or Path(relative).is_absolute()):
            raise HarnessError("Use a nonempty workspace-relative path.")
        raw = Path(relative)
        if ".." in raw.parts:
            raise HarnessError("Parent traversal is blocked.")
        for part in raw.parts:
            if protected_kind(part) is not None:
                raise HarnessError("Access to state, VCS metadata, or credential files is blocked.")
        candidate = self.root / raw
        current = self.root
        for part in raw.parts:
            current /= part
            if current.is_symlink():
                raise HarnessError("Symlink access is blocked.")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise HarnessError("Path is outside the workspace.")
        for location in self.protected_locations() if protected is None else protected:
            if resolved == location or resolved.is_relative_to(location):
                raise HarnessError("Access to a protected user configuration path is blocked.")
        if resolved.exists() and resolved.is_file() and resolved.stat().st_nlink > 1:
            raise HarnessError("Hard-linked files are blocked.")
        return resolved

    def protected_locations(self) -> list[Path]:
        """Resolved user configuration locations that file tools never reach.

        Protects credentials even when the workspace is a config directory or a
        user-selected ancestor of HOME / a custom XDG directory. A bounded walk
        takes one snapshot instead of recomputing it for every entry.
        """
        sensitive = [Path.home() / name for name in self.BLOCKED if name.startswith(".")]
        for variable in ("XDG_CONFIG_HOME", "CLOUDSDK_CONFIG", "GH_CONFIG_DIR", "AWS_SHARED_CREDENTIALS_FILE",
                         "GOOGLE_APPLICATION_CREDENTIALS", "KUBECONFIG"):
            value = os.environ.get(variable)
            if value:
                sensitive.extend(Path(item).expanduser() for item in value.split(os.pathsep) if item)
        return [location.resolve() for location in sensitive]

    def read(self, relative: str, limit: int = 100_000) -> str:
        path = self.path(relative)
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise HarnessError("Path must be a regular file.")
        if path.stat().st_size > limit:
            raise HarnessError(f"File exceeds the {limit:,}-byte limit.")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise HarnessError("File grew beyond its read limit.")
        if b"\x00" in data:
            raise HarnessError("Binary files are not supported.")
        return data.decode("utf-8")


STATE_NAMES = frozenset({".eira", ".codex"})
VCS_NAMES = frozenset({".git", ".hg", ".svn", ".bzr"})
SECRET_NAMES = frozenset({"id_rsa", "id_ed25519", "credentials", "credentials.json"})
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def protected_kind(part: str) -> str | None:
    """Classify one path component, case-insensitively, for file tools and the shell sandbox.

    'state' and 'vcs' names are blocked for file tools; 'secret' names are
    blocked for file tools and also masked inside shell containers.
    """
    lower = part.lower()
    if lower in STATE_NAMES:
        return "state"
    if lower in VCS_NAMES:
        return "vcs"
    if (lower in Workspace.BLOCKED or lower == ".env" or lower.startswith(".env.")
            or lower.endswith(SECRET_SUFFIXES) or lower in SECRET_NAMES):
        return "secret"
    return None


def atomic_write(path: Path, content: str, overwrite: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".eira-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        if overwrite:
            os.replace(temporary, path)
        else:
            # Link creation fails atomically if another writer created the path.
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast
