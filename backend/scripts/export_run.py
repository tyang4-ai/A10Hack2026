"""Export a finished run (state, events, page texts, never PDFs) to backend/data/replay/<run_id>/ for the replay fallback.

Usage (from the repo root): PY backend/scripts/export_run.py <run_id>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("B-cli")


if __name__ == "__main__":
    sys.exit(main())
