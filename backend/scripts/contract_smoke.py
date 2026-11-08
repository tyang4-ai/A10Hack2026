"""Contract smoke test: drive the API (real or mock) and validate every response against the contract models.

Usage (from the repo root): PY backend/scripts/contract_smoke.py --base <url> [--run <id>]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("B-store-api")


if __name__ == "__main__":
    sys.exit(main())
