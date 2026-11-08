"""Safe arithmetic evaluator for declared computations: + - * / parentheses, numbers, D-ids and names."""
from __future__ import annotations

from decimal import Decimal


def safe_eval(expr: str, env: dict[str, Decimal]) -> Decimal:
    raise NotImplementedError("B-verify")
