"""Check the frozen SHL plan: contract validation, ids, tiers, ranks, criterion coverage and every URL answering.

Usage (from the repo root): PY backend/scripts/check_plan.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("B-shl-plan")


if __name__ == "__main__":
    sys.exit(main())
