"""Head-and-tail shortening of long tool output, with full redacted copies saved for paging.

A shortened result keeps its beginning and its end, where test failures and
stack traces usually are. The complete text (redacted, best effort) is saved
under .eira/outputs/<session>/ so read_output can page or search it without
rerunning a slow command.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Callable

from .security import HarnessError, Redactor, atomic_write

OUTPUT_ID = re.compile(r"o-[0-9a-f]{12}")
SESSION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_SAVED_BYTES = 4 * 1024 * 1024
SESSION_CAP_BYTES = 64 * 1024 * 1024
MAX_AGE_SECONDS = 7 * 24 * 3600
SWEEP_LIMIT = 1_000
PAGE_LINES = 400
PAGE_CHARS = 24_000
MAX_MATCHES = 100
MATCH_CHARS = 500
BOUNDARY_WINDOW = 200
CUT_NOTE = "\n…[saved output cut at 4 MiB]\n"

_TERMINATOR = re.compile(r"\r\n|\r|\n")


def _terminators(text: str, start: int = 0, end: int | None = None) -> int:
    """Count line endings in text[start:end] by the split_lines rule, without building lists."""
    end = len(text) if end is None else end
    return text.count("\r", start, end) + text.count("\n", start, end) - text.count("\r\n", start, end)


def count_lines(text: str) -> int:
    """len(split_lines(text)), in constant memory."""
    return _terminators(text) + (1 if text and not text.endswith(("\n", "\r")) else 0)


def _line_of(text: str, index: int) -> int:
    """1-based line number of the character at index, by the split_lines rule."""
    line = _terminators(text, 0, index) + 1
    # findall stops at index, so a '\r\n' straddling it counted as a lone '\r'.
    if 0 < index < len(text) and text[index - 1] == "\r" and text[index] == "\n":
        line -= 1
    return line


def _head_cut(text: str, position: int) -> int:
    """The latest line start at or before position, within the window; else position."""
    best = None
    for match in _TERMINATOR.finditer(text, max(0, position - BOUNDARY_WINDOW), min(len(text), position + 1)):
        if match.end() <= position:
            best = match.end()
    return position if best is None else best


def _tail_cut(text: str, position: int) -> int:
    """The earliest line start at or after position, within the window; else position."""
    for match in _TERMINATOR.finditer(text, max(0, position - 1), min(len(text), position + BOUNDARY_WINDOW)):
        if position <= match.end() < len(text):
            return match.end()
    return position


def _iter_lines(text: str, first: int, last: int):
    """Yield (number, line, ending) for lines first..last, as split_lines numbers them."""
    number, start = 1, 0
    if first > 1:
        # Skip ahead without materializing earlier lines.
        for match in _TERMINATOR.finditer(text):
            number += 1
            start = match.end()
            if number == first:
                break
    if number != first:
        return
    for match in _TERMINATOR.finditer(text, start):
        if number > last:
            return
        yield number, text[start:match.start()], match.group()
        number, start = number + 1, match.end()
    if number <= last and start < len(text):
        yield number, text[start:], ""


def marker(omitted: int, first: int, last: int, output_id: str | None) -> str:
    if output_id:
        where = f'read_output(output_id="{output_id}", start_line={first}) shows them'
    else:
        where = "the full output could not be saved"
    return f"\n…[{omitted:,} characters truncated (lines {first:,}-{last:,}); {where}]…\n"


def head_tail(text: str, budget_chars: int, output_id: str | None = None) -> tuple[str, dict | None]:
    """Keep about budget_chars of text, half from each end, with one marker line between.

    Each cut moves inward to a line boundary when one is within 200
    characters, so the kept text never exceeds the budget. Returns the text
    unchanged and None when it already fits.
    """
    if len(text) <= budget_chars:
        return text, None
    half = budget_chars // 2
    head_end = _head_cut(text, half)
    tail_start = _tail_cut(text, len(text) - (budget_chars - half))
    first, last = _line_of(text, head_end), _line_of(text, tail_start - 1)
    info = {"omitted_chars": tail_start - head_end, "start_line": first, "end_line": last}
    return text[:head_end] + marker(tail_start - head_end, first, last, output_id) + text[tail_start:], info


def _json_len(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False)) - 2


class OutputStore:
    """Saved tool outputs for one session: redacted, private, size- and age-bounded."""

    def __init__(self, store_root: Path, session: str, redact: Callable[[str], str] | None = None):
        self.root = Path(store_root) / "outputs"
        self.session = session
        self.redact = redact or Redactor()

    @property
    def directory(self) -> Path:
        if not isinstance(self.session, str) or not SESSION_NAME.fullmatch(self.session):
            raise HarnessError("Saved outputs need a simple session ID.")
        return self.root / self.session

    @staticmethod
    def _private_dir(path: Path):
        if path.is_symlink():
            raise HarnessError("Saved output directories must not be symlinks.")
        path.mkdir(mode=0o700, exist_ok=True)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            raise HarnessError("Saved output directories must be real directories.")
        os.chmod(path, 0o700)

    def save(self, text: str, tool: str) -> dict:
        directory = self.directory
        if self.root.parent.is_symlink():
            raise HarnessError("The .eira state directory must not be a symlink.")
        self._private_dir(self.root)
        self._private_dir(directory)
        content = self.redact(text)
        data = content.encode("utf-8", "replace")
        if len(data) > MAX_SAVED_BYTES:
            data = data[:MAX_SAVED_BYTES - len(CUT_NOTE.encode())] + CUT_NOTE.encode()
        content = data.decode("utf-8", "ignore")
        output_id = "o-" + secrets.token_hex(6)
        path = directory / f"{output_id}.txt"
        atomic_write(path, content, overwrite=False)
        os.chmod(path, 0o600)
        os.chmod(directory, 0o700)  # In case a concurrent sweep removed and atomic_write recreated it.
        self._evict(directory, keep=path)
        self._sweep()
        return {"output_id": output_id, "bytes": len(content.encode()), "lines": count_lines(content)}

    @staticmethod
    def _entries(directory: Path) -> list[tuple[float, str, int, Path]]:
        found = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if OUTPUT_ID.fullmatch(entry.name.removesuffix(".txt")) and entry.name.endswith(".txt") \
                        and entry.is_file(follow_symlinks=False):
                    info = entry.stat(follow_symlinks=False)
                    found.append((info.st_mtime_ns, entry.name, info.st_size, Path(entry.path)))
        return sorted(found)

    def _evict(self, directory: Path, keep: Path):
        """Keep the session under its byte cap, removing the oldest outputs first."""
        entries = self._entries(directory)
        total = sum(size for _, _, size, _ in entries)
        for _, _, size, path in entries:
            if total <= SESSION_CAP_BYTES:
                break
            if path == keep:
                continue
            try:
                path.unlink()
                total -= size
            except FileNotFoundError:
                total -= size

    def _sweep(self):
        """Remove outputs older than the age limit from every session, bounded per call."""
        cutoff = time.time() - MAX_AGE_SECONDS
        seen = 0
        try:
            sessions = list(os.scandir(self.root))
        except OSError:
            return
        for session in sessions:
            if seen >= SWEEP_LIMIT:
                return
            if not session.is_dir(follow_symlinks=False):
                continue
            try:
                idle = session.stat(follow_symlinks=False).st_mtime < cutoff
                with os.scandir(session.path) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > SWEEP_LIMIT:
                            return
                        if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                            os.unlink(entry.path)
                if idle and session.name != self.session:
                    os.rmdir(session.path)  # Only succeeds once the directory is empty.
            except OSError:
                continue

    def _load(self, output_id) -> str:
        if not isinstance(output_id, str) or not OUTPUT_ID.fullmatch(output_id):
            raise HarnessError("output_id must be 'o-' followed by 12 lowercase hexadecimal characters.")
        missing = HarnessError(f"Output {output_id} is not available in this session (expired or never saved). "
                               "Rerun the command if needed.")
        directory = self.directory
        if directory.is_symlink() or self.root.is_symlink():
            raise missing
        try:
            fd = os.open(directory / f"{output_id}.txt", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            raise missing from None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SAVED_BYTES + 4096:
                raise missing
            return stream.read().decode("utf-8", "replace")

    def read(self, output_id, start_line: int = 1, end_line: int | None = None, query: str | None = None) -> dict:
        text = self._load(output_id)
        total = count_lines(text)
        if start_line < 1 or start_line > max(total, 1):
            raise HarnessError(f"start_line is past the end of the output ({total} lines).")
        if end_line is not None and end_line < start_line:
            raise HarnessError("end_line must not be before start_line.")
        last = total if end_line is None else min(end_line, total)
        if query is not None:
            return self._search(output_id, text, total, start_line, last, query)
        chunk, chars, cut_line = [], 0, False
        for _, line, ending in _iter_lines(text, start_line, last):
            cost = _json_len(line + ending)
            if chunk and (len(chunk) >= PAGE_LINES or chars + cost > PAGE_CHARS):
                break
            if not chunk and cost > PAGE_CHARS:
                keep = PAGE_CHARS
                while _json_len(line[:keep]) > PAGE_CHARS:
                    keep = keep * 3 // 4
                chunk, cut_line = [line[:keep]], True
                break
            chunk.append(line + ending)
            chars += cost
        shown_end = start_line - 1 + len(chunk)
        result = {"output_id": output_id, "content": "".join(chunk), "start_line": start_line,
                  "end_line": shown_end, "total_lines": total}
        if cut_line:
            result["line_truncated"] = True
            result["note"] = (f"Line {start_line} is longer than one page; only its start is shown. "
                              "Use query to find text within it.")
        if shown_end < last:
            result.update(truncated=True, next_start_line=shown_end + 1)
        return result

    @staticmethod
    def _search(output_id, text, total, start_line, last, query) -> dict:
        if not query:
            raise HarnessError("query must not be empty.")
        matches, truncated = [], False
        for number, line, _ in _iter_lines(text, start_line, last):
            column = line.find(query)
            if column < 0:
                continue
            if len(matches) >= MAX_MATCHES:
                truncated = True
                break
            # Show the text around the match, so a hit deep in a long line is visible.
            lo = max(0, column - 200) if len(line) > MATCH_CHARS else 0
            matches.append({"line": number, "text": line[lo:lo + MATCH_CHARS]})
        return {"output_id": output_id, "query": query, "matches": matches, "truncated": truncated,
                "total_lines": total}
