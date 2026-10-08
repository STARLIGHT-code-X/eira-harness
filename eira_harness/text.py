"""One line-splitting rule for every tool: only \\r\\n, \\r and \\n end a line."""
from __future__ import annotations

import re

_TERMINATOR = re.compile(r"\r\n|\r|\n")


def split_lines(text: str) -> list[tuple[str, str]]:
    """Return (line, ending) pairs; the joined pairs always equal the input.

    Unlike str.splitlines, \\x0b, \\x0c, \\x1c-\\x1e, \\x85, U+2028 and U+2029
    stay inside a line, as in editors, grep and patch tools. The last pair's
    ending is '' when the text has no final terminator.
    """
    pairs, start = [], 0
    for match in _TERMINATOR.finditer(text):
        pairs.append((text[start:match.start()], match.group()))
        start = match.end()
    if start < len(text):
        pairs.append((text[start:], ""))
    return pairs


def lines(text: str) -> list[str]:
    """Lines without their endings, split by the same rule as split_lines."""
    return [line for line, _ in split_lines(text)]
