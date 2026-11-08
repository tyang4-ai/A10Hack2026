"""Exact quote matching: normalization, quote location with page hints, and near-miss diffs."""
from __future__ import annotations

from ..contract import m


def normalize(s: str) -> str:
    """NFKC, whitespace runs become one space, nothing else."""
    raise NotImplementedError("B-verify")


def find_quote(page_text: str, quote: str) -> tuple[int, int] | None:
    """Offsets into the original page text."""
    raise NotImplementedError("B-verify")


def locate(pages: list[str], quote: str, page_hint: int) -> tuple[int, int, int] | None:
    """(page, start, end), hint page first."""
    raise NotImplementedError("B-verify")


def near_miss(page_text: str, quote: str) -> tuple[list[m.DiffSegment], tuple[int, int]]:
    """The diff and the closest real span."""
    raise NotImplementedError("B-verify")
