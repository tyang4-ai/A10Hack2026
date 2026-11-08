"""Pre-smoke: a local run with one source and a few pages, auto-approved, every built stage, then a summary.

Usage (from the repo root): PY backend/scripts/presmoke.py --source S-001 --pages a,b,c,d,e
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("B-extract")


if __name__ == "__main__":
    sys.exit(main())
