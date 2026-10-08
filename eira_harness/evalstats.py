"""Statistics for eval reports: pass@k, Wilson intervals, and an error taxonomy."""
from __future__ import annotations

from math import comb, sqrt
import re


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k from n samples with c passes (Chen et al. 2021, Eq. 1)."""
    if not 0 <= c <= n or k < 1:
        raise ValueError("Need 0 <= c <= n and k >= 1.")
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def wilson(passed: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (95% by default)."""
    if n <= 0:
        return 0.0, 0.0
    p = passed / n
    centre = p + z * z / (2 * n)
    spread = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    denominator = 1 + z * z / n
    # The bounds are exactly 0 and 1 at the extremes; avoid float noise there.
    low = 0.0 if passed == 0 else max(0.0, (centre - spread) / denominator)
    high = 1.0 if passed == n else min(1.0, (centre + spread) / denominator)
    return low, high


ERROR_CATEGORIES = [
    ("edit_miss", re.compile(r"old_string was not found|Failed to find expected lines|Failed to find context")),
    ("edit_ambiguous", re.compile(r"matches \d+ places|Ambiguous match")),
    ("syntax_rejected", re.compile(r"Syntax check failed")),
    ("approval_denied", re.compile(r"Action denied|Denied by read-only")),
    ("schema_error", re.compile(r"Invalid type for|Missing required tool arguments|Unknown tool|only declared properties")),
    ("stale_edit", re.compile(r"File changed|expected_sha256")),
]


def classify(error: str) -> str:
    """Map a tool error message to a fixed category, or 'other'."""
    for name, pattern in ERROR_CATEGORIES:
        if pattern.search(error or ""):
            return name
    return "other"
