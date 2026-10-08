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
            CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY, session TEXT NOT NULL, created TEXT NOT NULL, kind TEXT NOT NULL,
                label TEXT NOT NULL, user_seq INTEGER, message_seq INTEGER, manifest TEXT,
                files INTEGER NOT NULL, bytes INTEGER NOT NULL);
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

    def append(self, session: str, message: dict) -> int:
        with self.db:
            cursor = self.db.execute("INSERT INTO messages(session,payload) VALUES (?,?)",
                                     (session, self.encode(message)))
        return cursor.lastrowid

    def messages(self, session: str) -> list[dict]:
        return [message for _, message in self.message_rows(session)]

    def message_rows(self, session: str) -> list[tuple[int, dict]]:
        self.require(session)
        return [(row[0], json.loads(row[1])) for row in self.db.execute(
            "SELECT seq, payload FROM messages WHERE session=? ORDER BY seq", (session,))]

    @staticmethod
    def replay(rows: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
        """Replay the journal into the model's view.

        A normal row is appended. An ``eira_compaction`` row restarts the view.
        A rewind marker replaces the view with the view as it stood just before
        its ``to_seq`` row, which already reflects earlier markers. Markers are
        never part of the view. Views share tails, so memory stays linear.
        """
        before, state = {}, None
        for seq, message in rows:
            before[seq] = state
            if message.get("role") == "marker":
                rewind = message.get("eira_rewind")
                to_seq = rewind.get("to_seq") if isinstance(rewind, dict) else None
                if type(to_seq) is int and to_seq in before:
                    state = before[to_seq]
                continue
            state = ((seq, message), None if "eira_compaction" in message else state)
        view = []
        while state is not None:
            view.append(state[0])
            state = state[1]
        view.reverse()
        return view

    def model_messages(self, session: str) -> list[dict]:
        """Messages the model sees, replayed from the append-only journal.

        Compaction and rewinds never delete history. Earlier messages stay in
        the journal and in traces; only the model's view changes.
        """
        return [message for _, message in self.replay(self.message_rows(session))]

    def append_rewind(self, session: str, to_seq: int, checkpoint: str) -> int:
        if type(to_seq) is not int or not self.db.execute(
                "SELECT 1 FROM messages WHERE session=? AND seq=?", (session, to_seq)).fetchone():
            raise HarnessError("The rewind target is not a message in this session.")
        return self.append(session, {"role": "marker", "eira_rewind": {"to_seq": to_seq, "checkpoint": checkpoint}})

    def add_checkpoint(self, row: dict):
        self.require(row["session"])
        with self.db:
            self.db.execute("INSERT INTO checkpoints(id,session,created,kind,label,user_seq,message_seq,manifest,files,bytes) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (row["id"], row["session"], row["created"], row["kind"], self.redact(row["label"]),
                             row["user_seq"], row["message_seq"], row["manifest"], row["files"], row["bytes"]))

    def checkpoint(self, checkpoint_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        return dict(row) if row else None

    def checkpoints(self, session: str) -> list[dict]:
        self.require(session)
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM checkpoints WHERE session=? ORDER BY rowid", (session,))]

    def session_context(self, session: str) -> dict | None:
        row = self.db.execute("SELECT system, digest FROM session_context WHERE session=?", (session,)).fetchone()
        return dict(row) if row else None

    def set_session_context(self, session: str, system: str, digest: str):
        self.require(session)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO session_context VALUES (?,?,?)",
                            (session, self.redact(system), digest))

    def set_session_digest(self, session: str, digest: str):
        # The frozen prompt itself is never rewritten, even if redaction rules change.
        with self.db:
            self.db.execute("UPDATE session_context SET digest=? WHERE session=?", (digest, session))

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
