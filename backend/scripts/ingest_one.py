"""Fetch and ingest one source from data/shl/plan.json into a throwaway local run and print each page's first 400 characters.

Usage (from the repo root): PY backend/scripts/ingest_one.py <source_id> --pages a-b
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("B-ingest")


if __name__ == "__main__":
    sys.exit(main())
