"""Boundary checks and terminal/log hygiene. These are not an OS sandbox."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
import stat
import tempfile


class HarnessError(Exception):
    """A user-actionable error, safe to render without a traceback."""


def clean_terminal(text: str) -> str:
    # Strip terminal control sequences and bidi overrides from untrusted output.
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c in "\n\t" or
                   (ord(c) >= 32 and not 127 <= ord(c) <= 159
                    and c not in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))


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


class Workspace:
    BLOCKED = {".eira", ".git", ".ssh", ".aws", ".gnupg", ".kube", ".codex"}

    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise HarnessError("Workspace must be a directory.")

    def path(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise HarnessError("Use a nonempty workspace-relative path.")
        raw = Path(relative)
        if ".." in raw.parts:
            raise HarnessError("Parent traversal is blocked.")
        for part in raw.parts:
            lower = part.lower()
            if (lower in self.BLOCKED or lower == ".env" or lower.startswith(".env.")
                    or lower.endswith((".pem", ".key", ".p12", ".pfx"))
                    or lower in {"id_rsa", "id_ed25519", "credentials", "credentials.json"}):
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
        if resolved.exists() and resolved.is_file() and resolved.stat().st_nlink > 1:
            raise HarnessError("Hard-linked files are blocked.")
        return resolved

    def read(self, relative: str, limit: int = 100_000) -> str:
        path = self.path(relative)
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise HarnessError("Path must be a regular file.")
        if path.stat().st_size > limit:
            raise HarnessError(f"File exceeds the {limit:,}-byte limit.")
        data = path.read_bytes()
        if b"\x00" in data:
            raise HarnessError("Binary files are not supported.")
        return data.decode("utf-8")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".eira-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast
