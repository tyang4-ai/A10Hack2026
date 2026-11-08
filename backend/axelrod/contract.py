"""The shared contract: `m` is contract/models.py itself (never a copy), plus certainty, the BOFFIN rubric and the version.

contract/ goes on sys.path the same way mock/mock_server.py does it, so `m` here and `models` in the
mock are the same module object.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contract"
if str(CONTRACT_DIR) not in sys.path:
    sys.path.insert(0, str(CONTRACT_DIR))

import certainty  # noqa: E402  contract/certainty.py
import models as m  # noqa: E402  contract/models.py

if not hasattr(m, "EVENT_TYPES"):
    raise ImportError(f"'models' resolved to {getattr(m, '__file__', m)!r}, not contract/models.py")

RUBRIC: dict[str, Any] = json.loads((CONTRACT_DIR / "boffin_rubric.json").read_text(encoding="utf-8"))
CONTRACT_VERSION: str = (CONTRACT_DIR / "VERSION").read_text(encoding="utf-8").strip()

__all__ = ["m", "certainty", "RUBRIC", "CONTRACT_VERSION", "CONTRACT_DIR"]
