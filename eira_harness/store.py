"""SQLite conversation/event journal and a per-session process lock."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .security import HarnessError, Redactor


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, workspace: Path, redactor: Redactor | None = None):
        self.root = workspace.resolve() / ".eira"
        if self.root.is_symlink():
            raise HarnessError("The .eira state directory must not be a symlink.")
        self.root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        db_path = self.root / "state.db"
        if db_path.is_symlink() or (db_path.exists() and db_path.stat().st_nlink > 1):
            raise HarnessError("Unsafe state database path.")
        self.redact = redactor or Redactor()
        self.db = sqlite3.connect(db_path)
        os.chmod(db_path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, created TEXT NOT NULL, title TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY, session TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, session TEXT NOT NULL, time TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS session_context (
                session TEXT PRIMARY KEY, system TEXT NOT NULL, digest TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def encode(self, data) -> str:
        # Redact strings before encoding so escaping cannot hide secret values.
        def walk(value, depth=0):
            if depth > 32:
                raise HarnessError("Stored data exceeds the nesting limit.")
            if isinstance(value, str):
                return self.redact(value)
            if isinstance(value, list):
                return [walk(x, depth + 1) for x in value]
            if isinstance(value, dict):
                return {self.redact(str(k)): walk(v, depth + 1) for k, v in value.items()}
            return value
        return json.dumps(walk(data), ensure_ascii=False, allow_nan=False)

    def create(self, title: str) -> str:
        session = uuid.uuid4().hex[:16]
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                            (session, now(), self.redact(title[:120])))
        return session

    def require(self, session: str):
        if not re.fullmatch(r"[a-f0-9]{16}", session):
            raise HarnessError("Invalid session ID.")
        if not self.db.execute("SELECT 1 FROM sessions WHERE id=?", (session,)).fetchone():
            raise HarnessError("Session not found in this workspace.")

    @contextmanager
    def lock(self, session: str):
        self.require(session)
        try:
            import fcntl
        except ImportError as exc:
            raise HarnessError("Session locking requires Linux, macOS, or WSL.") from exc
        path = self.root / f"{session}.lock"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HarnessError("This session is already running in another process.") from exc
            yield
        finally:
            os.close(fd)

    def append(self, session: str, message: dict):
        with self.db:
            self.db.execute("INSERT INTO messages(session,payload) VALUES (?,?)",
                            (session, self.encode(message)))

    def messages(self, session: str) -> list[dict]:
        self.require(session)
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM messages WHERE session=? ORDER BY seq", (session,))]

    def model_messages(self, session: str) -> list[dict]:
        """Messages the model sees: everything since the latest compaction summary.

        Compaction never deletes history. Earlier messages stay in the journal
        and in traces; only the model's view starts at the summary.
        """
        history = self.messages(session)
        for index in range(len(history) - 1, -1, -1):
            if "eira_compaction" in history[index]:
                return history[index:]
        return history

    def session_context(self, session: str) -> dict | None:
        row = self.db.execute("SELECT system, digest FROM session_context WHERE session=?", (session,)).fetchone()
        return dict(row) if row else None

    def set_session_context(self, session: str, system: str, digest: str):
        self.require(session)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO session_context VALUES (?,?,?)",
                            (session, self.redact(system), digest))

    def event(self, session: str, kind: str, payload: dict):
        with self.db:
            self.db.execute("INSERT INTO events(session,time,kind,payload) VALUES (?,?,?,?)",
                            (session, now(), kind, self.encode(payload)))

    def events(self, session: str) -> list[dict]:
        self.require(session)
        return [dict(row) | {"payload": json.loads(row["payload"])} for row in self.db.execute(
            "SELECT seq,time,kind,payload FROM events WHERE session=? ORDER BY seq", (session,))]

    def sessions(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM sessions ORDER BY created DESC")]

    def remember(self, key: str, value: str):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", key) or len(value) > 2000:
            raise HarnessError("Memory needs a simple key (1–80 characters) and at most 2,000 characters.")
        if self.redact(key) != key or self.redact(value) != value:
            raise HarnessError("Memory cannot contain protected credentials.")
        if len(self.memories()) >= 50 and key not in self.memories():
            raise HarnessError("Memory is limited to 50 entries. Update or remove an existing entry.")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO memory VALUES(?,?,?)",
                            (key, self.redact(value), now()))

    def forget(self, key: str):
        with self.db:
            self.db.execute("DELETE FROM memory WHERE key=?", (key,))

    def memories(self) -> dict:
        return {row[0]: row[1] for row in self.db.execute("SELECT key,value FROM memory ORDER BY key")}
