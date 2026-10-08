"""Line search over parent-validated files; also runs standalone as the regex worker.

Python's re module has no timeout, so Eira never runs a model-supplied regular
expression in its own process. navigate.py starts this file as
``python -I search_worker.py`` with cwd '/' and an empty environment, writes one
JSON request to stdin, and kills the process group after a hard timeout.

The script imports only the standard library modules below, so -I (which keeps
the workspace and user site-packages off sys.path) leaves nothing from the
workspace importable. It reads only the absolute paths the parent validated with
Workspace.path; the check-then-read race is the same as for in-process reads.
os and stat are used to open without following a final symlink, without
blocking on a FIFO, and to recheck the opened file is a regular, singly linked
file.

Request: {pattern, flags, files: [absolute paths], max_results, context,
max_line_chars, output, max_bytes}. Response: the dict scan() returns.
"""
import json
import os
import re
import stat
import sys

BINARY_PROBE = 8192
TEXT_CHARS = 500
CONTEXT_CHARS = 300
SKIP_REASONS = ("binary", "too_large", "non_utf8", "unreadable")
_TERMINATOR = re.compile(r"\r\n|\r|\n")


def split_lines(text):
    """The same rule as eira_harness.text.split_lines: only \\r\\n, \\r and \\n end a line."""
    pairs, start = [], 0
    for match in _TERMINATOR.finditer(text):
        pairs.append((text[start:match.start()], match.group()))
        start = match.end()
    if start < len(text):
        pairs.append((text[start:], ""))
    return pairs


def read_text(path, max_bytes):
    """Return (text, None) or (None, reason) with reason in SKIP_REASONS."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None, "unreadable"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            return None, "unreadable"
        if info.st_size > max_bytes:
            return None, "too_large"
        chunks, total = [], 0
        while total <= max_bytes:
            data = os.read(fd, min(1 << 20, max_bytes + 1 - total))
            if not data:
                break
            chunks.append(data)
            total += len(data)
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        return None, "too_large"
    if b"\x00" in data[:BINARY_PROBE]:
        return None, "binary"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "non_utf8"


def _hit(index, lines, number, column, context):
    line = lines[number]
    # Show the text around the match, so a hit deep in a long line is visible.
    low = max(0, column - 200) if len(line) > TEXT_CHARS else 0
    hit = {"file": index, "line": number + 1, "column": column + 1, "text": line[low:low + TEXT_CHARS]}
    if context:
        hit["before"] = [item[:CONTEXT_CHARS] for item in lines[max(0, number - context):number]]
        hit["after"] = [item[:CONTEXT_CHARS] for item in lines[number + 1:number + 1 + context]]
    return hit


def scan(paths, match, output="matches", max_results=100, context=0, max_bytes=5_000_000,
         max_line_chars=None, split=split_lines, expired=None):
    """Search each file's lines with match(line) -> column or -1.

    Files are reported by their index in paths. expired() is polled between
    files and every 4,096 lines; when it returns True the scan stops truncated.
    """
    skipped = {reason: 0 for reason in SKIP_REASONS}
    hits, counts, total, searched, truncated = [], [], 0, 0, False
    for index, path in enumerate(paths):
        if expired is not None and expired():
            truncated = True
            break
        text, reason = read_text(path, max_bytes)
        if reason:
            skipped[reason] += 1
            continue
        searched += 1
        lines = [line for line, _ in split(text)]
        found, full = 0, False
        for number, line in enumerate(lines):
            if expired is not None and number % 4096 == 4095 and expired():
                truncated = full = True
                break
            column = match(line if max_line_chars is None else line[:max_line_chars])
            if column < 0:
                continue
            if output == "matches":
                if len(hits) >= max_results:
                    truncated = full = True
                    break
                hits.append(_hit(index, lines, number, column, context))
            found += 1
        if found:
            if output == "files" and len(counts) >= max_results:
                truncated = True
                break
            total += found
            counts.append([index, found])
        if full:
            break
    return {"hits": hits, "counts": counts, "total_matches": total, "files_searched": searched,
            "skipped": skipped, "truncated": truncated}


def main():
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    search = re.compile(request["pattern"], request["flags"]).search

    def match(line):
        found = search(line)
        return found.start() if found else -1

    result = scan(request["files"], match, request["output"], request["max_results"], request["context"],
                  request["max_bytes"], request["max_line_chars"])
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode("ascii"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
