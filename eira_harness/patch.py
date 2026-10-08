"""apply_patch: Codex-format patches, tolerant matching and all-or-nothing writes.

The parser ports the line state machine of codex-rs/apply-patch/src/streaming_parser.rs
(openai/codex at 8f21b7f) and keeps its error strings; matching ports seek_sequence.rs;
rebuilding text ports text_file.rs. Eira differs on purpose: Add File and Move to never
overwrite an existing file, a fuzzy match that fits several places fails instead of
guessing, and every file is validated, approved once and committed all-or-nothing with
rollback. docs/PATCHES.md describes the format and the recovery steps.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import difflib
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid

from .security import HarnessError, atomic_write, bounded_json_loads
from .text import lines as text_lines, split_lines

BEGIN = "*** Begin Patch"
END = "*** End Patch"
ADD = "*** Add File: "
DELETE = "*** Delete File: "
UPDATE = "*** Update File: "
MOVE = "*** Move to: "
END_OF_FILE = "*** End of File"
CONTEXT = "@@ "
EMPTY_CONTEXT = "@@"
ENVIRONMENT = "*** Environment ID:"

MAX_OPERATIONS = 100
MAX_CHUNKS = 2_000
MAX_PROBES = 50_000
VERIFY = "apply_patch verification failed: "
NOTHING = "No files were modified."
# Test hook: called with the number of index probes each closest-match search used.
probe_hook = None

# The model-visible tool description (static, so the cached prompt prefix stays stable).
DESCRIPTION = (
    "Edit files with a patch. Use it for multi-line, multi-hunk or multi-file changes, renames and deletions; "
    "use edit_file for one exact replacement. Format:\n"
    "*** Begin Patch\n"
    "*** Add File: <path>  (every following line starts with +)\n"
    "*** Delete File: <path>\n"
    "*** Update File: <path>\n"
    "*** Move to: <new path>  (optional, directly after Update File)\n"
    "@@ <optional line just above the change, e.g. def name():>\n"
    "<context lines start with a space, removed lines with -, added lines with +>\n"
    "*** End of File  (optional; the hunk ends at end of file)\n"
    "*** End Patch\n"
    "Show about 3 lines of context around each change and add an @@ line when context repeats. "
    "Paths are workspace-relative. The patch is validated, shown as one diff for approval and applied "
    "all-or-nothing. Do not re-read files after a successful patch.")

_VALID_HEADERS = ("Valid hunk headers: '*** Add File: {path}', '*** Delete File: {path}', "
                  "'*** Update File: {path}'")
_UNEXPECTED = ("Unexpected line found in update hunk: '{}'. Every line should start with ' ' "
               "(context line), '+' (added line), or '-' (removed line)")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    change_context: str | None = None
    old_lines: list = field(default_factory=list)
    new_lines: list = field(default_factory=list)
    # (old index, new index) of lines parsed as context, as in UpdateFileChunk.
    context_line_indices: list = field(default_factory=list)
    is_end_of_file: bool = False

    def push_context(self, line: str):
        self.context_line_indices.append((len(self.old_lines), len(self.new_lines)))
        self.old_lines.append(line)
        self.new_lines.append(line)

    def empty(self) -> bool:
        return not self.old_lines and not self.new_lines


@dataclass
class Hunk:
    kind: str  # "add", "delete" or "update"
    path: str
    contents: str = ""
    move_path: str | None = None
    chunks: list = field(default_factory=list)


def _patch_error(message: str) -> HarnessError:
    return HarnessError(f"invalid patch: {message}")


def _hunk_error(message: str, line_number: int) -> HarnessError:
    return HarnessError(f"invalid hunk at line {line_number}, {message}")


def _boundaries(rows: list[str]) -> list[str]:
    def strict(lines):
        first = lines[0].strip() if lines else None
        last = lines[-1].strip() if lines else None
        if first == BEGIN and last == END:
            return lines
        if first != BEGIN:
            raise _patch_error("The first line of the patch must be '*** Begin Patch'")
        raise _patch_error("The last line of the patch must be '*** End Patch'")
    try:
        return strict(rows)
    except HarnessError:
        # Lenient heredoc form, as Codex accepts for models that pass a heredoc as an argument.
        if (len(rows) >= 4 and rows[0] in ("<<EOF", "<<'EOF'", '<<"EOF"') and rows[-1].endswith("EOF")):
            return strict(rows[1:-1])
        raise


class _Parser:
    """Port of StreamingPatchParser's per-line state machine."""

    def __init__(self):
        self.mode = "not_started"
        self.hunks: list[Hunk] = []
        self.line_number = 0
        self.hunk_line_number = 0
        self.chunk_count = 0

    def _new_chunk(self, hunk: Hunk, **fields) -> Chunk:
        self.chunk_count += 1
        if self.chunk_count > MAX_CHUNKS:
            raise _patch_error(f"A patch may contain at most {MAX_CHUNKS:,} chunks; split it into smaller patches")
        chunk = Chunk(**fields)
        hunk.chunks.append(chunk)
        return chunk

    def _ensure_update_not_empty(self, line: str):
        if not self.hunks or self.hunks[-1].kind != "update":
            return
        hunk = self.hunks[-1]
        if not hunk.chunks and self.mode == "update":
            raise _hunk_error(f"Update file hunk for path '{hunk.path}' is empty", self.hunk_line_number)
        if hunk.chunks and hunk.chunks[-1].empty():
            if line == END:
                raise _hunk_error("Update hunk does not contain any lines", self.line_number)
            raise _hunk_error(_UNEXPECTED.format(line), self.line_number)

    def _headers(self, trimmed: str) -> bool:
        if self.mode == "started" and trimmed.startswith(ENVIRONMENT):
            raise _patch_error("Environment IDs are not supported")
        if trimmed == END:
            self._ensure_update_not_empty(trimmed)
            self.mode = "ended"
            return True
        for marker, kind in ((ADD, "add"), (DELETE, "delete"), (UPDATE, "update")):
            if trimmed.startswith(marker):
                self._ensure_update_not_empty(trimmed)
                if len(self.hunks) >= MAX_OPERATIONS:
                    raise _patch_error(f"A patch may contain at most {MAX_OPERATIONS} file operations; "
                                       "split it into smaller patches")
                self.hunks.append(Hunk(kind, trimmed[len(marker):]))
                self.mode = kind
                if kind == "update":
                    self.hunk_line_number = self.line_number
                return True
        return False

    def finish_line(self, line: str):
        """The last line: Codex's finish() compares it fully trimmed in every mode."""
        self.line_number += 1
        if line.strip() == END:
            self._ensure_update_not_empty(END)
            self.mode = "ended"
        else:
            self._process(line)

    def line(self, line: str):
        self.line_number += 1
        self._process(line)

    def _process(self, line: str):
        trimmed = line.strip()
        mode = self.mode
        if mode == "not_started":
            if trimmed == BEGIN:
                self.mode = "started"
                return
            raise _patch_error("The first line of the patch must be '*** Begin Patch'")
        if mode == "ended":
            if trimmed:
                raise _patch_error("The last line of the patch must be '*** End Patch'")
            return
        if mode in ("started", "delete"):
            if self._headers(trimmed):
                return
            raise _hunk_error(f"'{trimmed}' is not a valid hunk header. {_VALID_HEADERS}", self.line_number)
        if mode == "add":
            if self._headers(trimmed):
                return
            if line.startswith("+"):
                self.hunks[-1].contents += line[1:] + "\n"
                return
            raise _hunk_error(f"'{trimmed}' is not a valid hunk header. {_VALID_HEADERS}", self.line_number)
        # Update mode compares markers with only trailing whitespace removed, so an
        # indented context line is never mistaken for a header.
        update_line = line.rstrip()
        if self._headers(update_line):
            return
        hunk = self.hunks[-1]
        chunks = hunk.chunks
        is_context_marker = update_line == EMPTY_CONTEXT or update_line.startswith(CONTEXT)
        if chunks and chunks[-1].is_end_of_file:
            if not update_line:
                return
            if not is_context_marker:
                raise _hunk_error(f"Expected update hunk to start with a @@ context marker, got: '{line}'",
                                  self.line_number)
        if not chunks and hunk.move_path is None and update_line.startswith(MOVE):
            hunk.move_path = update_line[len(MOVE):]
            return
        if is_context_marker and chunks and chunks[-1].empty():
            raise _hunk_error(_UNEXPECTED.format(line), self.line_number)
        if update_line == EMPTY_CONTEXT:
            self._new_chunk(hunk)
            return
        if update_line.startswith(CONTEXT):
            self._new_chunk(hunk, change_context=update_line[len(CONTEXT):])
            return
        if update_line == END_OF_FILE:
            if chunks and chunks[-1].empty():
                raise _hunk_error("Update hunk does not contain any lines", self.line_number)
            if chunks:
                chunks[-1].is_end_of_file = True
            return
        if line == "" or line[0] in " +-":
            chunk = chunks[-1] if chunks else self._new_chunk(hunk)
            if line == "":
                chunk.push_context("")
            elif line[0] == " ":
                chunk.push_context(line[1:])
            elif line[0] == "+":
                chunk.new_lines.append(line[1:])
            else:
                chunk.old_lines.append(line[1:])
            return
        if chunks and not chunks[-1].empty():
            raise _hunk_error(f"Expected update hunk to start with a @@ context marker, got: '{line}'",
                              self.line_number)
        raise _hunk_error(_UNEXPECTED.format(line), self.line_number)


def parse(text: str) -> list[Hunk]:
    """Parse a patch into hunks; raises HarnessError with Codex's messages."""
    rows = [row[:-1] if row.endswith("\r") else row for row in text_lines(text.strip())]
    rows = _boundaries(rows)
    parser = _Parser()
    for index, row in enumerate(rows):
        if index == len(rows) - 1:
            parser.finish_line(row)
        else:
            parser.line(row)
    if parser.mode != "ended":
        raise _patch_error("The last line of the patch must be '*** End Patch'")
    return parser.hunks


# ---------------------------------------------------------------------------
# Matching (port of seek_sequence.rs, plus ambiguity detection)
# ---------------------------------------------------------------------------

_FOLD = str.maketrans({**dict.fromkeys("‐‑‒–—―−", "-"),
                       **dict.fromkeys("‘’‚‛", "'"),
                       **dict.fromkeys("“”„‟", '"'),
                       **dict.fromkeys("          "
                                       "  　", " ")})


def _fold(text: str) -> str:
    return text.strip().translate(_FOLD)


PASSES = (("exact", lambda text: text), ("rstrip", str.rstrip), ("strip", str.strip), ("unicode", _fold))


class _Matcher:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self._views: dict[int, list[str]] = {0: lines}

    def _view(self, number: int) -> list[str]:
        if number not in self._views:
            normalize = PASSES[number][1]
            self._views[number] = [normalize(line) for line in self.lines]
        return self._views[number]

    def positions(self, pattern: list[str], start: int, number: int):
        """Yield every start index >= start where pattern matches under pass `number`."""
        view = self._view(number)
        normalize = PASSES[number][1]
        wanted = [normalize(line) for line in pattern]
        size, last = len(wanted), len(view) - len(wanted)
        index = start
        while index <= last:
            try:
                index = view.index(wanted[0], index, last + 1)
            except ValueError:
                return
            if view[index:index + size] == wanted:
                yield index
            index += 1

    def search_start(self, pattern: list[str], start: int, eof: bool) -> int:
        if eof and len(self.lines) >= len(pattern):
            return max(len(self.lines) - len(pattern), start)
        return start

    def seek(self, pattern: list[str], start: int, eof: bool):
        """(index, pass number) of the first match, or None, as seek_sequence orders its passes."""
        if not pattern:
            return start, 0
        if len(pattern) > len(self.lines):
            return None
        begin = self.search_start(pattern, start, eof)
        for number in range(len(PASSES)):
            for index in self.positions(pattern, begin, number):
                return index, number
        return None


# ---------------------------------------------------------------------------
# Applying chunks (port of file_update.rs compute_replacements and text_file.rs)
# ---------------------------------------------------------------------------

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(,\d+)? \+")


def _visible(line: str) -> str:
    body = line.rstrip(" ")
    return (body + "·" * (len(line) - len(body))).replace("\t", "→")


def _closest(lines: list[str], pattern: list[str], path: str) -> str:
    """Up to three nearest windows, bounded to MAX_PROBES index probes."""
    file_norm = [_fold(line) for line in lines]
    index: dict[str, list[int]] = {}
    for number, text in enumerate(file_norm):
        index.setdefault(text, []).append(number)
    wanted = [_fold(line) for line in pattern]
    size, top = len(pattern), max(len(lines) - len(pattern), 0)
    probes, scores = 0, Counter()
    present = []
    for offset, text in enumerate(wanted):
        probes += 1
        if text and text in index:
            present.append(offset)
    # Rare lines first, so common ones ('}', 'return') cannot exhaust the budget alone.
    present.sort(key=lambda offset: (len(index[wanted[offset]]), offset))
    for offset in present:
        for position in index[wanted[offset]]:
            if probes >= MAX_PROBES:
                break
            probes += 1
            scores[min(max(position - offset, 0), top)] += 1
        if probes >= MAX_PROBES:
            break
    if not scores:
        first = next(((offset, text) for offset, text in enumerate(wanted) if text), None)
        if first is not None and probes < MAX_PROBES:
            choices = list(index)[:MAX_PROBES - probes]
            probes += len(choices)
            for match in difflib.get_close_matches(first[1], choices, n=3, cutoff=0.6):
                scores[min(max(index[match][0] - first[0], 0), top)] += 1
    if probe_hook is not None:
        probe_hook(probes)
    best = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:20]
    ratio = {start: difflib.SequenceMatcher(None, lines[start:start + size], pattern).ratio() for start, _ in best}
    ranked = sorted(best, key=lambda item: (-ratio[item[0]], -item[1], item[0]))[:3]
    out = []
    for start, _ in ranked:
        window = lines[start:start + size]
        first_line, last_line = start + 1, start + len(window)
        rows = difflib.unified_diff([_visible(line) for line in window], [_visible(line) for line in pattern],
                                    f"{path} lines {first_line}-{last_line}", "patch", n=0, lineterm="")
        # Hunk headers count file lines from the top of the file, not from the window.
        diff = "\n".join(_HUNK_HEADER.sub(lambda m: f"@@ -{int(m.group(1)) + start}{m.group(2) or ''} +", row)
                         if row.startswith("@@") else row for row in rows)
        if not diff:
            diff = ("(identical text, but before an earlier hunk's match or away from the end of file "
                    "that *** End of File requires; hunks must be in file order)")
        if len(diff) > 2_000:
            diff = diff[:1_999] + "…"
        out.append(f"Closest match at lines {first_line}-{last_line} (similarity {ratio[start]:.2f}):\n{diff}")
    return "\n".join(out)


def apply_chunks(content: str, path: str, chunks: list[Chunk]):
    """Return (new content, info) or raise HarnessError; info has fuzz, warnings, first_changed_line."""
    pairs = split_lines(content)
    lines = [text for text, _ in pairs]
    matcher = _Matcher(lines)
    replacements, fuzz, warnings = [], [], []
    cursor = 0
    for number, chunk in enumerate(chunks, 1):
        if chunk.change_context is not None:
            found = matcher.seek([chunk.change_context], cursor, False)
            if found is None:
                raise HarnessError(f"Failed to find context '{chunk.change_context}' in {path}")
            cursor = found[0] + 1
        if not chunk.old_lines:
            replacements.append((len(lines), 0, list(chunk.new_lines)))
            continue
        pattern, new_slice = chunk.old_lines, chunk.new_lines
        found = matcher.seek(pattern, cursor, chunk.is_end_of_file)
        if found is None and pattern[-1] == "":
            # The trailing empty line stands for the file's final newline.
            pattern = pattern[:-1]
            if new_slice and new_slice[-1] == "":
                new_slice = new_slice[:-1]
            found = matcher.seek(pattern, cursor, chunk.is_end_of_file)
        if found is None:
            report = _closest(lines, chunk.old_lines, path)
            raise HarnessError(f"Failed to find expected lines in {path}:\n" + "\n".join(chunk.old_lines)
                               + (f"\n{report}" if report else "") + f"\n{NOTHING}")
        start, pass_number = found
        if pattern and chunk.change_context is None:
            begin = matcher.search_start(pattern, cursor, chunk.is_end_of_file)
            places = []
            for index in matcher.positions(pattern, begin, pass_number):
                places.append(index + 1)
                if len(places) > 10:
                    break
            if len(places) > 1:
                shown = ", ".join(str(place) for place in places[:10]) + (", ..." if len(places) > 10 else "")
                count = f"{len(places)}+" if len(places) > 10 else str(len(places))
                if pass_number:
                    raise HarnessError(f"Ambiguous match: hunk {number} matches {count} places in {path} "
                                       f"(lines {shown}). Add an @@ line or more context.")
                warnings.append(f"Hunk {number} in {path} matches {count} places (lines {shown}); it was applied "
                                f"at line {places[0]}. Add an @@ line or more context if another place was meant.")
        if pass_number:
            fuzz.append({"path": path, "hunk": number, "pass": PASSES[pass_number][0]})
        # Context lines stay in place, byte for byte, with their own terminators.
        old_start = new_start = 0
        for old_context, new_context in chunk.context_line_indices:
            if old_context >= len(pattern) or new_context >= len(new_slice):
                break
            if old_start != old_context or new_start != new_context:
                replacements.append((start + old_start, old_context - old_start, new_slice[new_start:new_context]))
            old_start, new_start = old_context + 1, new_context + 1
        if old_start != len(pattern) or new_start != len(new_slice):
            replacements.append((start + old_start, len(pattern) - old_start, new_slice[new_start:]))
        cursor = start + len(pattern)
    replacements.sort(key=lambda item: item[0])
    preferred = next((ending for _, ending in pairs if ending), "\n")
    out, source = [], 0
    for start, old_len, new_lines in replacements:
        out.extend(pairs[source:start])
        out.extend((text, preferred) for text in new_lines)
        source = start + old_len
    out.extend(pairs[source:])
    # Every line gets a terminator: apply_patch's historical trailing-newline rule.
    new_content = "".join(text + (ending or preferred) for text, ending in out)
    info = {"fuzz": fuzz, "warnings": warnings}
    if replacements:
        info["first_changed_line"] = replacements[0][0] + 1
    return new_content, info


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class Change:
    op: str  # add, update, delete, move
    path: str
    old: str | None
    new: str | None
    to: str | None = None
    mode: int | None = None


class _Plan:
    def __init__(self, toolbox, base: str):
        from .tools import MAX_TEXT_FILE
        self.toolbox, self.workspace, self.base = toolbox, toolbox.workspace, base
        self.max_bytes = MAX_TEXT_FILE
        self.initial: dict[str, str | None] = {}
        self.current: dict[str, str | None] = {}
        self.modes: dict[str, int] = {}
        self.order: list[str] = []
        self.moves: list[tuple[str, str]] = []
        self.files: list[dict] = []
        self.summary = {"A": [], "M": [], "D": []}
        self.fuzz: list[dict] = []
        self.warnings: list[str] = []

    def resolve(self, raw: str) -> tuple[str, Path]:
        root = str(self.workspace.root)
        if raw.startswith(root + "/"):
            raw = raw[len(root) + 1:]
        elif raw.startswith("/workspace/"):
            raw = raw[len("/workspace/"):]
        elif raw.startswith("/") or os.path.isabs(raw):
            raise HarnessError(f"Path is outside the workspace: {raw}")
        elif self.base:
            raw = f"{self.base}/{raw}"
        target = self.workspace.path(raw)
        relative = target.relative_to(self.workspace.root).as_posix()
        if relative in ("", "."):
            raise HarnessError("Use a nonempty workspace-relative path.")
        return relative, target

    def _track(self, rel: str, target: Path, exists: bool):
        if rel in self.current:
            return
        content = None
        if exists:
            try:
                content = self.workspace.read(rel, self.max_bytes)
            except UnicodeDecodeError:
                raise HarnessError(f"{rel} is not UTF-8 text.") from None
            except OSError as exc:
                raise HarnessError(f"Cannot read {rel}: {exc.strerror or exc}.") from None
            self.toolbox._check_editable(content)
            self.modes[rel] = stat.S_IMODE(target.stat().st_mode)
        self.initial[rel] = self.current[rel] = content
        self.order.append(rel)

    def _present(self, rel: str, target: Path) -> bool:
        if rel in self.current:
            return self.current[rel] is not None
        return os.path.lexists(target)

    def add(self, hunk: Hunk):
        rel, target = self.resolve(hunk.path)
        if self._present(rel, target):
            raise HarnessError(f"Add File target already exists: {rel}. Use *** Update File, or Delete File and "
                               "Add File in one patch.")
        self._track(rel, target, False)
        self.current[rel] = hunk.contents
        self.summary["A"].append(rel)
        self.files.append({"path": rel, "op": "add", "_at": rel})

    def delete(self, hunk: Hunk):
        rel, target = self.resolve(hunk.path)
        if rel not in self.current:
            if target.is_dir():
                raise HarnessError(f"Failed to delete file {rel}: it is a directory")
            if not os.path.lexists(target):
                raise HarnessError(f"Failed to delete file {rel}: the file does not exist")
            self._track(rel, target, True)
        if self.current[rel] is None:
            raise HarnessError(f"Failed to delete file {rel}: the file does not exist")
        self.current[rel] = None
        self.summary["D"].append(rel)
        self.files.append({"path": rel, "op": "delete"})

    def update(self, hunk: Hunk):
        rel, target = self.resolve(hunk.path)
        if rel not in self.current:
            if target.is_dir():
                raise HarnessError(f"Failed to read file to update {rel}: it is a directory")
            if not os.path.lexists(target):
                raise HarnessError(f"Failed to read file to update {rel}: the file does not exist")
            self._track(rel, target, True)
        content = self.current[rel]
        if content is None:
            raise HarnessError(f"Failed to read file to update {rel}: the file does not exist")
        new, info = apply_chunks(content, rel, hunk.chunks)
        self.fuzz += info["fuzz"]
        self.warnings += info["warnings"]
        destination = rel
        if hunk.move_path is not None:
            destination, dest_target = self.resolve(hunk.move_path)
            if destination != rel:
                if self._present(destination, dest_target):
                    raise HarnessError(f"Move destination already exists: {destination}. Delete it earlier in the "
                                       "same patch, or choose another path.")
                self._track(destination, dest_target, False)
        if destination != rel:
            self.current[rel] = None
            self.current[destination] = new
            self.moves.append((rel, destination))
            self.files.append({"path": rel, "op": "move", "to": destination, "_at": destination})
        else:
            self.current[rel] = new
            entry = {"path": rel, "op": "update", "_at": rel}
            if "first_changed_line" in info:
                entry["first_changed_line"] = info["first_changed_line"]
            self.files.append(entry)
        self.summary["M"].append(destination)

    def changes(self) -> list[Change]:
        """Net effect per path, in the order the patch first touched each path."""
        sources, destinations = {}, {}
        for source, destination in self.moves:
            if (isinstance(self.initial[source], str) and self.current[source] is None
                    and self.initial[destination] is None and isinstance(self.current[destination], str)
                    and not {source, destination} & (set(sources) | set(destinations))):
                sources[source], destinations[destination] = destination, source
        changes = []
        for rel in self.order:
            old, new = self.initial[rel], self.current[rel]
            if rel in destinations:
                continue
            if rel in sources:
                to = sources[rel]
                changes.append(Change("move", rel, old, self.current[to], to=to, mode=self.modes.get(rel)))
            elif old is None and new is not None:
                changes.append(Change("add", rel, None, new))
            elif old is not None and new is None:
                changes.append(Change("delete", rel, old, None, mode=self.modes.get(rel)))
            elif old is not None and old != new:
                changes.append(Change("update", rel, old, new, mode=self.modes.get(rel)))
        return changes

    def validate(self, changes: list[Change]) -> list[dict]:
        checks = []
        for change in changes:
            target = change.to or change.path
            if change.new is not None:
                self.toolbox._check_editable(change.new)
                if len(change.new.encode()) > self.max_bytes:
                    raise HarnessError(f"{target} would exceed the {self.max_bytes:,}-byte limit.")
                checks += self.toolbox.check_write(target, change.old, change.new)
        return checks


def _detail(changes: list[Change]) -> str:
    from .tools import _diff
    count = Counter(change.op for change in changes)
    parts = [f"apply_patch: {len(changes)} files ({count['add']} added, {count['update']} modified, "
             f"{count['delete']} deleted, {count['move']} moved)\n"]
    for change in changes:
        if change.op == "add":
            parts.append(_diff(change.path, "", change.new) or f"Create empty file: {change.path}\n")
        elif change.op == "update":
            parts.append(_diff(change.path, change.old, change.new))
        elif change.op == "delete":
            parts.append(_diff(change.path, change.old, "") or f"Delete empty file: {change.path}\n")
        else:
            parts.append(f"rename from {change.path}\nrename to {change.to}\n" + _diff(change.to, change.old, change.new))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def _private_write(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _write_manifest(directory: Path, manifest: dict):
    temporary = directory / "manifest.json.tmp"
    if temporary.exists():
        temporary.unlink()
    _private_write(temporary, json.dumps(manifest, indent=1).encode())
    os.replace(temporary, directory / "manifest.json")


def _default_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno, type(exc).__name__) if exc.errno else type(exc).__name__


def _patches_root(store_root: Path) -> Path:
    if store_root.is_symlink():
        raise HarnessError("The .eira state directory must not be a symlink.")
    store_root.mkdir(mode=0o700, exist_ok=True)
    patches = store_root / "patches"
    if patches.is_symlink():
        raise HarnessError("The .eira/patches directory must not be a symlink.")
    patches.mkdir(mode=0o700, exist_ok=True)
    os.chmod(patches, 0o700)
    return patches


def _commit(toolbox, changes: list[Change]):
    root = toolbox.workspace.root
    directory = None
    try:
        patches = _patches_root(Path(toolbox.store.root))
        patch_id = uuid.uuid4().hex[:12]
        (patches / patch_id).mkdir(mode=0o700)
        directory = patches / patch_id
        os.chmod(directory, 0o700)
        for name in ("pre", "trash"):
            (directory / name).mkdir(mode=0o700)
        ops = []
        for number, change in enumerate(changes):
            op = {"op": change.op, "path": change.path}
            if change.to:
                op["to"] = change.to
            if change.old is not None:
                _private_write(directory / "pre" / str(number), change.old.encode())
                op.update(pre_image=f"pre/{number}", mode=change.mode, sha256_before=_sha(change.old))
            if change.new is not None:
                op["sha256_after"] = _sha(change.new)
            ops.append(op)
        manifest = {"version": 1, "state": "committing", "id": patch_id, "workspace": str(root), "ops": ops,
                    "temporary_files": [], "created_directories": []}
        _write_manifest(directory, manifest)
    except OSError as exc:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise HarnessError(f"Could not prepare the patch journal ({_errno_name(exc)}). {NOTHING}") from None

    temps: dict[int, Path] = {}
    created: list[Path] = []
    done: list[tuple] = []
    current = changes[0].path
    try:
        # (2) New contents go to temporary files beside their targets.
        for number, change in enumerate(changes):
            if change.new is None:
                continue
            current = change.to or change.path
            target = root / current
            parent = root
            for part in Path(current).parts[:-1]:
                parent = parent / part
                if not os.path.lexists(parent):
                    os.mkdir(parent)
                    created.append(parent)
            fd, name = tempfile.mkstemp(prefix=".eira-patch-", dir=target.parent)
            temps[number] = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(change.new.encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, change.mode if change.mode is not None else _default_mode())
        manifest["temporary_files"] = [str(path.relative_to(root)) for path in temps.values()]
        manifest["created_directories"] = [str(path.relative_to(root)) for path in created]
        _write_manifest(directory, manifest)

        # (3) Apply in order. Each step is one atomic rename or link.
        def trash(number, path):
            current_trash = directory / "trash" / str(number)
            try:
                os.replace(path, current_trash)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                os.unlink(path)  # the pre-image is already saved
                return None
            return current_trash

        for number, change in enumerate(changes):
            current = change.to or change.path
            if change.op == "update":
                os.replace(temps[number], root / change.path)
                temps.pop(number)
                done.append(("replace", change))
            elif change.op in ("add", "move"):
                os.link(temps[number], root / current)
                done.append(("link", change, root / current))
                os.unlink(temps.pop(number))
            if change.op in ("delete", "move"):
                current = change.path
                moved_to = trash(number, root / change.path)
                done.append(("trash", change, root / change.path, moved_to))
    except OSError as exc:
        _rollback(toolbox, directory, manifest, done, temps, created, current, exc)
    manifest["state"] = "committed"
    try:
        _write_manifest(directory, manifest)
    except OSError:
        pass
    shutil.rmtree(directory, ignore_errors=True)


def _rollback(toolbox, directory, manifest, done, temps, created, path, error):
    failures = []
    for step in reversed(done):
        kind, change = step[0], step[1]
        try:
            if kind == "replace":
                target = toolbox.workspace.root / change.path
                atomic_write(target, change.old, overwrite=True)
                if change.mode is not None:
                    os.chmod(target, change.mode)
            elif kind == "link":
                target = step[2]
                if target.is_file() and not target.is_symlink():
                    if hashlib.sha256(target.read_bytes()).hexdigest() == _sha(change.new):
                        target.unlink()
            else:
                target, moved_to = step[2], step[3]
                if os.path.lexists(target):
                    raise FileExistsError(errno.EEXIST, "path was recreated", str(target))
                if moved_to is not None:
                    os.replace(moved_to, target)
                else:
                    atomic_write(target, change.old, overwrite=False)
                    if change.mode is not None:
                        os.chmod(target, change.mode)
        except OSError as exc:
            failures.append({"path": change.path, "error": _errno_name(exc)})
    for temporary in temps.values():
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            failures.append({"path": str(temporary), "error": _errno_name(exc)})
    for folder in reversed(created):
        try:
            folder.rmdir()
        except OSError:
            pass
    name = _errno_name(error)
    if not failures:
        shutil.rmtree(directory, ignore_errors=True)
        toolbox.notify("patch_rolled_back", patch_id=manifest["id"], path=path, error=name)
        raise HarnessError(f"Patch failed while writing {path} ({name}); all changes were rolled back.") from None
    manifest.update(state="rollback_failed", failures=failures, error={"path": path, "errno": name})
    try:
        _write_manifest(directory, manifest)
    except OSError:
        pass
    toolbox.notify("patch_rollback_failed", patch_id=manifest["id"], path=path, error=name,
                   directory=str(directory))
    raise HarnessError(f"Patch failed while writing {path} ({name}) and the rollback was incomplete. "
                       f"Pre-images and a manifest are kept in {directory}; see docs/PATCHES.md "
                       "to restore by hand.") from None


def leftovers(store_root) -> list[dict]:
    """Patch journals left by an interrupted commit or a failed rollback."""
    patches = Path(store_root) / "patches"
    found = []
    try:
        if Path(store_root).is_symlink() or patches.is_symlink() or not patches.is_dir():
            return []
        entries = sorted(os.scandir(patches), key=lambda entry: entry.name)
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        state = "unknown"
        manifest = Path(entry.path) / "manifest.json"
        try:
            if not manifest.is_symlink() and manifest.stat().st_size <= 2_000_000:
                state = str(bounded_json_loads(manifest.read_text(encoding="utf-8")).get("state", "unknown"))
        except (OSError, ValueError, AttributeError, HarnessError):
            pass
        if state != "committed":
            found.append({"id": entry.name, "state": state, "directory": entry.path})
    return found


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _summary(plan: _Plan) -> str:
    rows = [f"{kind} {path}" for kind in ("A", "M", "D") for path in plan.summary[kind]]
    return "\n".join(["Success. Updated the following files:"] + rows)


def _base(toolbox, directory: str | None) -> str:
    if not directory:
        return ""
    root = str(toolbox.workspace.root)
    if directory in ("/workspace", root):
        return ""
    if directory.startswith(root + "/"):
        directory = directory[len(root) + 1:]
    elif directory.startswith("/workspace/"):
        directory = directory[len("/workspace/"):]
    elif directory.startswith("/"):
        raise HarnessError(f"Path is outside the workspace: {directory}")
    target = toolbox.workspace.path(directory)
    if not target.is_dir():
        raise HarnessError(f"cd: {directory}: not a workspace directory")
    relative = target.relative_to(toolbox.workspace.root).as_posix()
    return "" if relative == "." else relative


def apply(toolbox, text: str, directory: str | None = None, routed_from: str | None = None) -> dict:
    """Validate every file, ask once, recheck, then commit all files or none."""
    try:
        hunks = parse(text)
        if not hunks:
            raise HarnessError(NOTHING)
        plan = _Plan(toolbox, _base(toolbox, directory))
        for hunk in hunks:
            getattr(plan, hunk.kind)(hunk)
        changes = plan.changes()
        checks = plan.validate(changes)
    except HarnessError as exc:
        raise HarnessError(VERIFY + str(exc)) from None
    if changes:
        always_ask = any(toolbox.review_paths(path) for path in plan.order)
        toolbox.policy.require("apply_patch", _detail(changes), workspace_write=True, always_ask=always_ask)
        # Recheck every touched path after the human approval wait.
        for rel in plan.order:
            try:
                target = toolbox.workspace.path(rel)
                before = plan.initial[rel]
                if before is None:
                    stale = os.path.lexists(target)
                else:
                    stale = _sha(toolbox.workspace.read(rel, plan.max_bytes)) != _sha(before)
            except (HarnessError, OSError, UnicodeError):
                stale = True
            if stale:
                raise HarnessError(f"File changed during approval; patch cancelled. {NOTHING}")
        _commit(toolbox, changes)
    else:
        plan.warnings.append("The patch leaves every file unchanged; nothing was written.")
    files = []
    for entry in plan.files:
        at = entry.pop("_at", None)
        if at is not None and isinstance(plan.current.get(at), str):
            entry["sha256"] = _sha(plan.current[at])
        files.append(entry)
    result = {"summary": _summary(plan), "files": files, "fuzz": plan.fuzz, "warnings": plan.warnings,
              "checks": checks}
    if routed_from:
        result["routed_from"] = routed_from
    return result


def describe(arguments: dict) -> str:
    text = arguments.get("input")
    if not isinstance(text, str):
        return ""
    paths = re.findall(r"^[ \t]*\*\*\* (?:Add|Delete|Update) File: (.+?)\s*$", text, re.M)
    if not paths:
        return "patch"
    return f"{len(paths)} file{'s' if len(paths) != 1 else ''}: {', '.join(paths)}"


# Shell routing: `[cd DIR &&] apply_patch <<'EOF' ... EOF` and `apply_patch '<patch>'`,
# anchored at both ends so nothing can follow the patch.
_HEREDOC = re.compile(
    r"\A\s*(?:cd[ \t]+(?P<dir>[A-Za-z0-9_./@%+=:,-]+)[ \t]*&&[ \t]*)?(?:apply_patch|applypatch)[ \t]*"
    r"<<(?P<dash>-?)[ \t]*(?P<q>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)[ \t]*\n"
    r"(?P<body>.*?)\n(?P<indent>[ \t]*)(?P=tag)[ \t]*\n?\s*\Z", re.S)
_QUOTED = re.compile(r"\A\s*(?:apply_patch|applypatch)[ \t]+(?P<q>['\"])(?P<body>\*\*\* Begin Patch.*)(?P=q)\s*\Z",
                     re.S)


def from_shell_command(command) -> dict | None:
    """Return {'input', 'directory'} when a shell command is only an apply_patch invocation."""
    if not isinstance(command, str):
        return None
    match = _HEREDOC.match(command)
    if match:
        dash, tag, body = match.group("dash"), match.group("tag"), match.group("body")
        indent = match.group("indent")
        if (dash and indent.strip("\t")) or (not dash and indent):
            return None
        rows = body.split("\n")
        if dash:
            rows = [row.lstrip("\t") for row in rows]
        # A real shell would end the heredoc at the first terminator and run what follows.
        if any(row.rstrip(" \t") == tag for row in rows):
            return None
        return {"input": "\n".join(rows), "directory": match.group("dir")}
    match = _QUOTED.match(command)
    if match:
        quote, body = match.group("q"), match.group("body")
        if quote in body or (quote == '"' and any(char in body for char in "$`\\")):
            return None
        return {"input": body, "directory": None}
    return None
