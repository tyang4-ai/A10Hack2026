"""Number parsing, scale conversion and display rounding (round half up, never format)."""
from __future__ import annotations

from decimal import Decimal


def parse_number(s: str) -> Decimal | None:
    """"1,234" "(1,234)" "−5.2" "€1.2 billion" "12.5%"."""
    raise NotImplementedError("B-verify")


def to_base(value: Decimal, scale: str) -> Decimal:
    raise NotImplementedError("B-verify")


def numbers_in_text(text: str) -> list[tuple[str, Decimal]]:
    """Every number in text, skipping years and fiscal-year labels."""
    raise NotImplementedError("B-verify")


def half_up(x: Decimal | float, places: int = 1) -> str:
    raise NotImplementedError("B-verify")
