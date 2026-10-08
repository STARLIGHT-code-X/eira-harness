"""Workspace checkpoints in a private content-addressed store, and user-initiated rewinds.

Snapshots never run git and never read or write the user's .git. File bytes
are stored once, named by their SHA-256, under .eira/history/objects; a
checkpoint's manifest is an object too, and the SQLite ``checkpoints`` table
indexes them by session. Conversation rewinds only append a journal marker,
so the model's next request is a prefix of an earlier one.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid

from .security import HarnessError, Workspace
from .store import Store, now

HEAVY_DIRS = frozenset({"node_modules", ".venv", "venv", "__pycache__", ".tox", ".nox", ".mypy_cache",
                        ".pytest_cache", ".ruff_cache", ".gradle", ".next", ".turbo"})
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES = 100_000
MAX_SECONDS = 10.0
KEEP_PER_SESSION = 100
KEEP_DAYS = 30
STORE_CAP_BYTES = 2 * 1024 ** 3
GC_SECONDS = 2.0
DIFF_FILE_LINES = 200
DIFF_TOTAL_BYTES = 200_000
DIFF_MAX_FILE = 5_000_000
# A file modified this close to (or after) the checkpoint that recorded it is
# re-read next time even if its stat matches: timestamps can be coarse.
RACY_NS = 2_000_000_000
LIST_NAMES = 50

_HASH = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"ck-[0-9a-f]{10}")


class SnapshotSkipped(HarnessError):
    def __init__(self, reason: str):
        super().__init__(f"Workspace snapshot skipped: {reason.replace('_', ' ')} exceeded.")
        self.reason = reason


def _opener(path, flags):
    # Never follow a symlink and never block on a FIFO swapped in after the scan.
    return os.open(path, flags | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))


def _read_workspace_file(path: Path, limit: int) -> tuple[bytes, os.stat_result] | None:
    """Read a regular file without following symlinks; None if it is not one any more."""
    with open(path, "rb", opener=_opener) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        data = stream.read(limit + 1)
    return data, info


def atomic_write_bytes(path: Path, data: bytes, mode: int) -> None:
    """Write bytes beside the target, fsync, set the mode, then rename over it."""
    fd, temporary = tempfile.mkstemp(prefix=".eira-restore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode & 0o777)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _created_ns(created: str) -> int:
    try:
        return int(datetime.fromisoformat(created).timestamp() * 1_000_000_000)
    except (TypeError, ValueError):
        return 0


def _size(value: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(value)


def _names(paths: list[str]) -> str:
    shown = ", ".join(paths[:LIST_NAMES])
    return shown + (f", … {len(paths) - LIST_NAMES} more" if len(paths) > LIST_NAMES else "")


def _text(data: bytes | None) -> str | None:
    if data is None:
        return ""
    if len(data) > DIFF_MAX_FILE or b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class Checkpoints:
    """Snapshots before mutating tool batches, plus listing, diff and rewind."""

    def __init__(self, store: Store, workspace: Workspace):
        self.store, self.workspace = store, workspace
        self.history = store.root / "history"
        self._lock_depth, self._lock_fd = 0, None
        self._manifests: dict[str, dict] = {}
        self._turn: dict | None = None
        self._written = 0
        self._orphans = False

    # ----- store -----------------------------------------------------------------

    def _ensure(self) -> Path:
        """Create the private store; refuse symlinks anywhere on the way."""
        for directory in (self.store.root, self.history, self.history / "objects"):
            if directory.is_symlink():
                raise HarnessError("The checkpoint store must not be a symlink.")
            directory.mkdir(mode=0o700, exist_ok=True)
            if not directory.is_dir() or directory.is_symlink():
                raise HarnessError("The checkpoint store path is not a directory.")
            os.chmod(directory, 0o700)
        return self.history / "objects"

    @contextmanager
    def _locked(self):
        """Store-wide lock: snapshots, GC and restores never interleave across sessions."""
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        self._ensure()
        import fcntl
        fd = os.open(self.history / ".lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._lock_depth = 1
            yield
        finally:
            self._lock_depth = 0
            os.close(fd)

    def _object_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise HarnessError("Invalid checkpoint object name.")
        return self.history / "objects" / digest[:2] / digest[2:]

    def _put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._object_path(digest)
        if path.is_symlink():
            raise HarnessError("The checkpoint store must not contain symlinks.")
        if path.exists():
            return digest
        objects = self._ensure()
        bucket = objects / digest[:2]
        if bucket.is_symlink():
            raise HarnessError("The checkpoint store must not contain symlinks.")
        bucket.mkdir(mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=bucket)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
        self._written += len(data)
        return digest

    def _get(self, digest: str) -> bytes:
        path = self._object_path(digest)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as exc:
            raise HarnessError(f"Checkpoint object {digest[:12]} is missing; the checkpoint cannot be used.") from exc
        except OSError as exc:
            raise HarnessError(f"Checkpoint object {digest[:12]} cannot be read safely.") from exc
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise HarnessError("Checkpoint objects must be regular files.")
            data = stream.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise HarnessError(f"Checkpoint object {digest[:12]} is corrupt; the checkpoint cannot be used.")
        return data

    def _put_manifest(self, manifest: dict) -> str:
        data = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        digest = self._put(data)
        self._manifests[digest] = manifest
        return digest

    def manifest(self, digest: str) -> dict:
        if digest not in self._manifests:
            manifest = json.loads(self._get(digest))
            if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("entries"), dict):
                raise HarnessError("Unsupported checkpoint manifest.")
            if len(self._manifests) > 16:
                self._manifests.clear()
            self._manifests[digest] = manifest
        return self._manifests[digest]

    # ----- scanning --------------------------------------------------------------

    def _previous(self) -> tuple[dict, int]:
        """Entries of the latest stored manifest for this root, and their racy cutoff."""
        row = self.store.db.execute("SELECT manifest, created FROM checkpoints WHERE manifest IS NOT NULL "
                                    "ORDER BY rowid DESC LIMIT 1").fetchone()
        if not row:
            return {}, 0
        try:
            manifest = self.manifest(row["manifest"])
        except (HarnessError, ValueError, OSError):
            return {}, 0
        if manifest.get("root") != str(self.workspace.root):
            return {}, 0
        return manifest["entries"], _created_ns(row["created"]) - RACY_NS

    def scan(self, keep: bool) -> dict:
        """Manifest of every in-scope file; with keep, file bytes are stored as objects."""
        previous, cutoff = self._previous()
        deadline = time.monotonic() + MAX_SECONDS
        root = self.workspace.root
        entries: dict[str, dict] = {}
        skipped = {"too_large": [], "protected": 0, "heavy_dirs": 0, "symlinks": 0}
        pending = [""]
        while pending:
            relative_dir = pending.pop()
            try:
                iterator = os.scandir(root / relative_dir if relative_dir else root)
            except OSError:
                skipped["protected"] += 1
                continue
            with iterator:
                for entry in iterator:
                    if time.monotonic() > deadline:
                        raise SnapshotSkipped("time_limit")
                    relative = f"{relative_dir}/{entry.name}" if relative_dir else entry.name
                    try:
                        if entry.is_symlink():
                            skipped["symlinks"] += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in HEAVY_DIRS:
                                skipped["heavy_dirs"] += 1
                                continue
                            self.workspace.path(relative)
                            pending.append(relative)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        self.workspace.path(relative)
                        info = entry.stat(follow_symlinks=False)
                    except HarnessError:
                        skipped["protected"] += 1
                        continue
                    except OSError:
                        continue
                    if info.st_size > MAX_FILE_BYTES:
                        skipped["too_large"].append(relative)
                        continue
                    if len(entries) >= MAX_FILES:
                        raise SnapshotSkipped("file_limit")
                    mode = stat.S_IMODE(info.st_mode)
                    old = previous.get(relative)
                    if (isinstance(old, dict) and old.get("size") == info.st_size and old.get("mtime_ns") == info.st_mtime_ns
                            and old.get("ino") == info.st_ino and old.get("mode") == mode
                            and info.st_mtime_ns < cutoff and isinstance(old.get("sha256"), str)
                            and (not keep or self._object_path(old["sha256"]).exists())):
                        entries[relative] = dict(old)
                        continue
                    try:
                        read = _read_workspace_file(root / relative, MAX_FILE_BYTES)
                    except OSError:
                        continue
                    if read is None:
                        continue
                    data, info = read
                    if len(data) > MAX_FILE_BYTES:
                        skipped["too_large"].append(relative)
                        continue
                    digest = self._put(data) if keep else hashlib.sha256(data).hexdigest()
                    # The stat taken with the read: a later change alters it and forces a re-read.
                    entries[relative] = {"sha256": digest, "size": len(data), "mode": stat.S_IMODE(info.st_mode),
                                         "mtime_ns": info.st_mtime_ns, "ino": info.st_ino}
        skipped["too_large"].sort()
        return {"version": 1, "root": str(root), "entries": dict(sorted(entries.items())),
                "skipped": skipped, "complete": True}

    def _snapshot(self, session: str, kind: str, label: str, *, row_id: str | None = None,
                  user_seq: int | None = None, message_seq: int | None = None, created: str | None = None,
                  skip_if_unchanged: bool = False) -> dict | None:
        """Store a full snapshot and record or fill its row; never leaves a partial row."""
        created = created or now()
        with self._locked():
            try:
                manifest = self.scan(keep=True)
            except BaseException:
                self._orphans = True
                raise
            digest = self._put_manifest(manifest)
            files = len(manifest["entries"])
            size = sum(entry["size"] for entry in manifest["entries"].values())
            if skip_if_unchanged:
                last = self.store.db.execute("SELECT manifest FROM checkpoints WHERE session=? AND manifest IS NOT NULL "
                                             "ORDER BY rowid DESC LIMIT 1", (session,)).fetchone()
                if last and last["manifest"] == digest:
                    return None
            if row_id is not None:
                with self.store.db:
                    self.store.db.execute("UPDATE checkpoints SET manifest=?, files=?, bytes=? WHERE id=?",
                                          (digest, files, size, row_id))
                row = self.store.checkpoint(row_id)
            else:
                row = {"id": "ck-" + uuid.uuid4().hex[:10], "session": session, "created": created, "kind": kind,
                       "label": label, "user_seq": user_seq, "message_seq": message_seq, "manifest": digest,
                       "files": files, "bytes": size}
                self.store.add_checkpoint(row)
        return row

    def snapshot(self, session: str, label: str = "manual snapshot") -> dict:
        """Take a step checkpoint now; raises SnapshotSkipped or HarnessError instead of emitting events."""
        self.store.require(session)
        row = self._snapshot(session, "step", label)
        self.gc()
        return row

    # ----- agent hooks -------------------------------------------------------------

    def begin_turn(self, session: str, prompt: str, user_seq: int, event=lambda kind, **payload: None) -> str | None:
        """Record a turn boundary; its manifest is filled by the run's first snapshot."""
        self._turn = None
        try:
            label = " ".join(prompt.split())[:80]
            row = {"id": "ck-" + uuid.uuid4().hex[:10], "session": session, "created": now(), "kind": "turn",
                   "label": label, "user_seq": user_seq, "message_seq": None, "manifest": None,
                   "files": 0, "bytes": 0}
            self.store.add_checkpoint(row)
        except Exception as exc:  # A checkpoint problem never stops the run.
            event("checkpoint_failed", reason=str(exc)[:500] or type(exc).__name__)
            return None
        self._turn = {"id": row["id"], "session": session, "filled": False}
        return row["id"]

    def before_batch(self, toolbox, calls: list[dict], message_seq: int | None,
                     event=lambda kind, **payload: None) -> str | None:
        """Snapshot before a batch that can change the workspace; failures never block it."""
        if toolbox.policy.read_only:
            return None
        names = [(call.get("function") or {}).get("name") for call in calls if isinstance(call, dict)]
        if not any(isinstance(name, str) and name in toolbox.registry and toolbox.mutating(name) for name in names):
            return None
        session = toolbox.session
        turn = self._turn if self._turn and self._turn["session"] == session else None
        pending = turn["id"] if turn and not turn["filled"] else None
        if pending:
            # One attempt only: a later snapshot would show this turn's own changes.
            turn["filled"] = True
        try:
            if pending:
                row = self._snapshot(session, "turn", "", row_id=pending)
            else:
                row = self._snapshot(session, "step", f"before tools at message {message_seq}",
                                     message_seq=message_seq, skip_if_unchanged=True)
            if row is not None:
                event("checkpoint_created", checkpoint=row["id"], type=row["kind"], files=row["files"], bytes=row["bytes"])
        except SnapshotSkipped as exc:
            event("checkpoint_skipped", reason=exc.reason, **({"checkpoint": pending} if pending else {}))
            row = None
        except Exception as exc:
            event("checkpoint_failed", reason=str(exc)[:500] or type(exc).__name__,
                  **({"checkpoint": pending} if pending else {}))
            row = None
        try:
            self.gc()
        except Exception:
            pass  # Retention is retried after the next snapshot.
        return row["id"] if row else None

    # ----- retention ---------------------------------------------------------------

    def _walk_objects(self, deadline: float) -> dict[str, tuple[Path, int]] | None:
        objects = self.history / "objects"
        found = {}
        if not objects.is_dir() or objects.is_symlink():
            return found
        with os.scandir(objects) as buckets:
            for bucket in buckets:
                if not re.fullmatch(r"[0-9a-f]{2}", bucket.name) or not bucket.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(bucket.path) as items:
                    for item in items:
                        if time.monotonic() > deadline:
                            return None
                        if re.fullmatch(r"[0-9a-f]{62}", item.name) and item.is_file(follow_symlinks=False):
                            found[bucket.name + item.name] = (Path(item.path), item.stat(follow_symlinks=False).st_size)
        return found

    def _reachable(self, deadline: float) -> set[str] | None:
        reachable = set()
        for (digest,) in self.store.db.execute("SELECT DISTINCT manifest FROM checkpoints WHERE manifest IS NOT NULL"):
            if time.monotonic() > deadline:
                return None
            reachable.add(digest)
            try:
                manifest = self.manifest(digest)
            except (HarnessError, ValueError, OSError):
                continue
            reachable.update(entry.get("sha256") for entry in manifest["entries"].values() if isinstance(entry, dict))
        return reachable

    def gc(self, budget: float = GC_SECONDS):
        """Apply retention: per-session count, age, then the total size cap."""
        deadline = time.monotonic() + budget
        with self._locked():
            db = self.store.db
            cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
            with db:
                removed = db.execute("DELETE FROM checkpoints WHERE created < ?", (cutoff,)).rowcount
                crowded = db.execute("SELECT session FROM checkpoints GROUP BY session HAVING COUNT(*) > ?",
                                     (KEEP_PER_SESSION,)).fetchall()
                for (session,) in crowded:
                    removed += db.execute("DELETE FROM checkpoints WHERE rowid IN (SELECT rowid FROM checkpoints "
                                          "WHERE session=? ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
                                          (session, KEEP_PER_SESSION)).rowcount
            if not (removed or self._written or self._orphans):
                return
            sizes = self._walk_objects(deadline)
            if sizes is None:
                return
            reachable = self._reachable(deadline)
            if reachable is None:
                return
            total = sum(size for digest, (_, size) in sizes.items() if digest in reachable)
            while total > STORE_CAP_BYTES and time.monotonic() < deadline:
                rows = [row[0] for row in db.execute("SELECT rowid FROM checkpoints ORDER BY rowid")]
                if len(rows) <= 1:
                    break  # Always keep the newest checkpoint.
                drop = rows[:max(1, (len(rows) - 1) // 10)]
                with db:
                    db.executemany("DELETE FROM checkpoints WHERE rowid=?", [(rowid,) for rowid in drop])
                reachable = self._reachable(deadline)
                if reachable is None:
                    return
                total = sum(size for digest, (_, size) in sizes.items() if digest in reachable)
            for digest, (path, _) in sizes.items():
                if digest not in reachable:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    self._manifests.pop(digest, None)
            self._written, self._orphans = 0, False

    # ----- lookup ------------------------------------------------------------------

    def default_session(self) -> str | None:
        row = self.store.db.execute("SELECT session FROM checkpoints ORDER BY rowid DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def _prompt_numbers(self, session: str) -> tuple[dict[int, int], dict[int, str]]:
        """Turn numbers count the session's prompts in journal order, so retention never renumbers them."""
        from .agent import UPDATE_NOTE
        numbers, texts = {}, {}
        for seq, message in self.store.message_rows(session):
            content = message.get("content")
            if (message.get("role") == "user" and "eira_compaction" not in message and isinstance(content, str)
                    and not content.startswith(UPDATE_NOTE)):
                numbers[seq] = len(numbers) + 1
                texts[seq] = content
        return numbers, texts

    def listing(self, session: str) -> list[dict]:
        numbers, _ = self._prompt_numbers(session)
        out = []
        for row in self.store.checkpoints(session):
            item = dict(row)
            item["turn"] = numbers.get(row["user_seq"]) if row["kind"] == "turn" else None
            item["snapshot"] = row["manifest"] is not None
            out.append(item)
        return out

    def resolve(self, session: str, target: str) -> dict:
        target = (target or "").strip()
        rows = self.listing(session)
        if _ID.fullmatch(target):
            for row in rows:
                if row["id"] == target:
                    return row
            raise HarnessError(f"Checkpoint {target} was not found in session {session}.")
        match = re.fullmatch(r"(?:turn:)?(\d{1,9})", target)
        if match:
            number = int(match.group(1))
            for row in rows:
                if row["kind"] == "turn" and row["turn"] == number:
                    return row
            raise HarnessError(f"Turn {number} has no checkpoint (checkpoints were off, or retention removed it). "
                               "Run `eira checkpoints` to list them.")
        raise HarnessError("Choose a checkpoint as ck-<10 hex digits> or turn:N.")

    def _effective(self, session: str, row: dict) -> tuple[dict | None, str]:
        """The checkpoint whose manifest represents a row's state, with an explanation."""
        if row["manifest"] is not None:
            return row, ""
        for event in self.store.events(session):
            if (event["kind"] in {"checkpoint_failed", "checkpoint_skipped"}
                    and event["payload"].get("checkpoint") == row["id"]):
                raise HarnessError(f"The snapshot for {row['id']} was not taken ({event['kind'].split('_')[1]}), "
                                   "so its files cannot be restored.")
        later = self.store.db.execute("SELECT * FROM checkpoints WHERE session=? AND rowid > (SELECT rowid FROM checkpoints "
                                      "WHERE id=?) AND manifest IS NOT NULL ORDER BY rowid LIMIT 1",
                                      (session, row["id"])).fetchone()
        name = f"turn {row['turn']}" if row.get("turn") else row["id"]
        if later is None:
            return None, (f"{name.capitalize()} changed no files and no later checkpoint exists, "
                          "so Eira has not changed files since; files are left as they are.")
        return dict(later), (f"{name.capitalize()} changed no files, so the code state used is the one just before "
                             f"Eira's next change ({later['id']}).")

    # ----- planning ------------------------------------------------------------------

    def _current(self) -> dict:
        try:
            return self.scan(keep=False)
        except SnapshotSkipped as exc:
            raise HarnessError(f"The workspace is too large to compare ({exc.reason.replace('_', ' ')}).") from exc

    @staticmethod
    def _changes(target: dict, current: dict) -> tuple[list[str], list[str], list[str]]:
        wanted, present = target["entries"], current["entries"]
        untracked = set(target.get("skipped", {}).get("too_large", []))
        restore, recreate = [], []
        for path, entry in wanted.items():
            now_entry = present.get(path)
            if now_entry is None:
                recreate.append(path)
            elif now_entry["sha256"] != entry["sha256"] or (now_entry["mode"] & 0o777) != (entry["mode"] & 0o777):
                restore.append(path)
        delete = [path for path in present if path not in wanted and path not in untracked]
        return sorted(restore), sorted(recreate), sorted(delete)

    def _validate(self, writes: list[str], delete: list[str]):
        """Check every path before the first write, so a bad path changes nothing."""
        doomed = set(delete)
        for relative in delete:
            self.workspace.path(relative)
        for relative in writes:
            path = self.workspace.path(relative)
            if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
                raise HarnessError(f"Cannot restore {relative}: a directory or special file is in its place.")
            parts = relative.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                location = self.workspace.root / parent
                if os.path.lexists(location) and not location.is_dir() and parent not in doomed:
                    raise HarnessError(f"Cannot restore {relative}: {parent} is not a directory.")

    def _read_current(self, relative: str) -> bytes | None:
        try:
            read = _read_workspace_file(self.workspace.path(relative), DIFF_MAX_FILE)
        except (HarnessError, OSError):
            return None
        return read[0] if read else None

    def _diff_text(self, pairs: list[tuple[str, bytes | None, bytes | None]], per_file: int | None,
                   total: int | None) -> str:
        from .tools import _diff
        out, used = [], 0
        for path, old, new in pairs:
            before, after = _text(old), _text(new)
            if before is None or after is None:
                block = f"Binary or large file {path} differs\n"
            else:
                block = _diff(path, before, after) or f"{path}: mode change only\n"
                if per_file is not None:
                    lines = block.splitlines(keepends=True)
                    if len(lines) > per_file:
                        block = "".join(lines[:per_file]) + f"… {len(lines) - per_file} more diff lines for {path}\n"
            if total is not None and used + len(block.encode()) > total:
                out.append(f"… diff output capped at {total // 1000} KB; {len(pairs) - len(out)} more files not shown\n")
                break
            out.append(block)
            used += len(block.encode())
        return "".join(out)

    def plan(self, session: str, target: str, mode: str) -> dict:
        if mode not in {"code", "conversation", "both"}:
            raise HarnessError("Choose --code, --conversation, or --both.")
        row = self.resolve(session, target)
        plan = {"session": session, "checkpoint": row["id"], "kind": row["kind"], "turn": row.get("turn"),
                "label": row["label"], "mode": mode, "source": None, "note": "", "restore": [], "recreate": [],
                "delete": [], "untracked": [], "not_restorable": [], "diff": "", "hidden_messages": 0,
                "to_seq": None, "prompt": None}
        if mode in {"conversation", "both"}:
            if row["kind"] != "turn" or row["user_seq"] is None:
                raise HarnessError(f"{row['id']} is a {row['kind']} checkpoint; conversation rewinds need a turn "
                                   "boundary, so choose a turn checkpoint (turn:N) or use --code.")
            plan["to_seq"] = row["user_seq"]
            plan["hidden_messages"] = self._hidden(session, row["user_seq"])
            plan["prompt"] = self._prompt_numbers(session)[1].get(row["user_seq"])
        if mode in {"code", "both"}:
            with self._locked():
                source, plan["note"] = self._effective(session, row)
                if source is not None:
                    plan["source"] = source["id"]
                    plan["manifest"] = source["manifest"]
                    target_manifest = self.manifest(source["manifest"])
                    current = self._current()
                    plan["restore"], plan["recreate"], plan["delete"] = self._changes(target_manifest, current)
                    plan["untracked"] = [p for p in current["skipped"]["too_large"] if p not in target_manifest["entries"]]
                    plan["not_restorable"] = list(target_manifest.get("skipped", {}).get("too_large", []))
                    self._validate(plan["restore"] + plan["recreate"], plan["delete"])
                    pairs = [(p, self._read_current(p), self._get(target_manifest["entries"][p]["sha256"]))
                             for p in plan["restore"] + plan["recreate"]]
                    pairs += [(p, self._read_current(p), b"") for p in plan["delete"]]
                    plan["diff"] = self._diff_text(sorted(pairs, key=lambda item: item[0]), DIFF_FILE_LINES, DIFF_TOTAL_BYTES)
        return plan

    def _hidden(self, session: str, to_seq: int) -> int:
        rows = self.store.message_rows(session)
        visible = {seq for seq, _ in Store.replay(rows)}
        marker = (rows[-1][0] + 1 if rows else 1, {"role": "marker", "eira_rewind": {"to_seq": to_seq}})
        after = {seq for seq, _ in Store.replay([*rows, marker])}
        return len(visible - after)

    @staticmethod
    def summary(plan: dict) -> str:
        what = {"code": "code", "conversation": "conversation", "both": "code and conversation"}[plan["mode"]]
        where = f"turn {plan['turn']}" if plan.get("turn") else plan["kind"]
        lines = [f"Rewind {what} to {plan['checkpoint']} ({where}: {plan['label'] or 'no label'})"]
        if plan["mode"] in {"code", "both"}:
            if plan["note"]:
                lines.append(plan["note"])
            changes = [("restore", plan["restore"]), ("recreate", plan["recreate"]), ("delete", plan["delete"])]
            if plan["source"] and not any(paths for _, paths in changes):
                lines.append("Files: already match the checkpoint; nothing to restore.")
            for verb, paths in changes:
                if paths:
                    lines.append(f"  {verb:<8} {len(paths)} file{'s' if len(paths) != 1 else ''}: {_names(paths)}")
            if plan["untracked"]:
                lines.append(f"  left in place: {len(plan['untracked'])} file(s) over {_size(MAX_FILE_BYTES)} "
                             f"that checkpoints do not track: {_names(plan['untracked'])}")
            if plan["not_restorable"]:
                lines.append(f"  not restorable: {len(plan['not_restorable'])} file(s) were over {_size(MAX_FILE_BYTES)} "
                             f"at the checkpoint and are left as they are: {_names(plan['not_restorable'])}")
        if plan["mode"] in {"conversation", "both"}:
            count = plan["hidden_messages"]
            lines.append(f"Conversation: {count} message{'s' if count != 1 else ''} will be hidden from the model; "
                         "the journal keeps them.")
        if plan["diff"]:
            lines += ["", plan["diff"].rstrip("\n")]
        return "\n".join(lines)

    # ----- rewind --------------------------------------------------------------------

    def rewind(self, session: str, target: str, mode: str, confirm, emit=None) -> dict | None:
        """Plan, confirm, back up, restore and journal a user-initiated rewind.

        Holds the session lock throughout, so no run of this session can
        interleave. Returns None when the user declines.
        """
        with self.store.lock(session):
            plan = self.plan(session, target, mode)
            if not confirm(self.summary(plan)):
                return None
            restored = deleted = 0
            if mode in {"code", "both"} and plan["source"] is not None:
                restored, deleted = self._restore(session, plan)
            if mode in {"conversation", "both"}:
                plan["hidden_messages"] = self._hidden(session, plan["to_seq"])
                self._rewind_conversation(session, plan)
            payload = {"checkpoint": plan["checkpoint"], "mode": mode, "restored": restored, "deleted": deleted,
                       "hidden_messages": plan["hidden_messages"] if mode != "code" else 0}
            self.store.event(session, "rewind_completed", payload)
            if emit is not None:
                emit({"event": "rewind_completed", "session": session, **payload})
            return {**payload, "prompt": plan["prompt"], "backup": plan.get("backup")}

    def _restore(self, session: str, plan: dict) -> tuple[int, int]:
        with self._locked():
            if self.store.checkpoint(plan["source"]) is None:
                raise HarnessError("The checkpoint was removed by retention; nothing was restored.")
            try:
                backup = self._snapshot(session, "rewind-backup", f"before rewind to {plan['checkpoint']}")
            except SnapshotSkipped as exc:
                raise HarnessError(f"Could not back up the current files ({exc.reason.replace('_', ' ')}); "
                                   "nothing was restored.") from exc
            plan["backup"] = backup["id"]
            target = self.manifest(plan["manifest"])
            current = self.manifest(backup["manifest"])
            if self._changes(target, current) != (plan["restore"], plan["recreate"], plan["delete"]):
                raise HarnessError("Files changed while the rewind waited for confirmation; nothing was restored. "
                                   f"Run the rewind again (backup {backup['id']}).")
            writes = plan["restore"] + plan["recreate"]
            self._validate(writes, plan["delete"])
            for relative in writes:  # Every object must be present and intact before the first change.
                self._get(target["entries"][relative]["sha256"])
            for relative in plan["delete"]:
                path = self.workspace.path(relative)
                if os.path.lexists(path) and stat.S_ISREG(os.lstat(path).st_mode):
                    path.unlink()
            for relative in writes:
                entry = target["entries"][relative]
                path = self.workspace.path(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path = self.workspace.path(relative)
                atomic_write_bytes(path, self._get(entry["sha256"]), entry["mode"])
            return len(writes), len(plan["delete"])

    def _rewind_conversation(self, session: str, plan: dict):
        from .agent import UPDATE_NOTE
        rows = self.store.message_rows(session)
        before = {seq for seq, _ in Store.replay(rows)}
        self.store.append_rewind(session, plan["to_seq"], plan["checkpoint"])
        after = {seq for seq, _ in Store.replay(self.store.message_rows(session))}
        hidden = [message for seq, message in rows if seq in before - after]
        # Guidance or memory updates delivered by hidden messages must be sent again.
        if any(UPDATE_NOTE in (message.get("content") or "") for message in hidden
               if message.get("role") == "user" and isinstance(message.get("content"), str)):
            self.store.set_session_digest(session, "")

    # ----- diff ------------------------------------------------------------------------

    def diff(self, session: str, target: str | None = None, stat_only: bool = False) -> str:
        if target:
            row = self.resolve(session, target)
        else:
            rows = self.listing(session)
            if not rows:
                raise HarnessError("This session has no checkpoints yet.")
            row = rows[0]
        with self._locked():
            source, note = self._effective(session, row)
            if source is None:
                return note
            manifest = self.manifest(source["manifest"])
            current = self._current()
            changed = [p for p in manifest["entries"] if p in current["entries"]
                       and current["entries"][p]["sha256"] != manifest["entries"][p]["sha256"]]
            added = [p for p in current["entries"] if p not in manifest["entries"]]
            removed = [p for p in manifest["entries"] if p not in current["entries"]]
            pairs = sorted([(p, self._get(manifest["entries"][p]["sha256"]), self._read_current(p)) for p in changed]
                           + [(p, b"", self._read_current(p)) for p in added]
                           + [(p, self._get(manifest["entries"][p]["sha256"]), b"") for p in removed],
                           key=lambda item: item[0])
        header = f"Changes since {source['id']}" + (f" ({note})" if note else "")
        if not pairs:
            return header + ": none."
        if not stat_only:
            return header + "\n" + self._diff_text(pairs, None, None).rstrip("\n")
        lines = [header]
        status = {p: "M" for p in changed} | {p: "A" for p in added} | {p: "D" for p in removed}
        for path, old, new in pairs:
            before, after = _text(old), _text(new)
            if before is None or after is None:
                lines.append(f"{status[path]}  {path}  (binary)")
                continue
            from .tools import _diff
            body = _diff(path, before, after).splitlines()[2:]
            plus = sum(1 for line in body if line.startswith("+"))
            minus = sum(1 for line in body if line.startswith("-"))
            lines.append(f"{status[path]}  {path}  +{plus} -{minus}")
        lines.append(f"{len(pairs)} file{'s' if len(pairs) != 1 else ''} changed")
        return "\n".join(lines)

    @staticmethod
    def format_listing(rows: list[dict]) -> str:
        if not rows:
            return "No checkpoints in this session yet."
        lines = []
        for row in rows:
            where = f"turn {row['turn']}" if row.get("turn") else row["kind"]
            files = f"{row['files']} files, {_size(row['bytes'])}" if row["snapshot"] else "no changes yet"
            lines.append(f"{row['id']}  {where:<14} {row['created'][:19].replace('T', ' ')}  {files:<24} {row['label']}")
        return "\n".join(lines)
