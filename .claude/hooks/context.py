#!/usr/bin/env python3
"""SessionStart hook: puts the build board back in front of the orchestrator.

Runs on startup, resume, clear and after compaction. On the backend branch it
adds backend/status.md, the last commits and the uncommitted files to the
session context, so the orchestrator picks up where it left off. On any other
branch it prints nothing.

New code, written for SCU ACM AWS x A10 Hack 2026.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2]).resolve()
MAX_STATUS = 9000


def git(*args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(PROJECT), *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    if git("rev-parse", "--abbrev-ref", "HEAD") != "backend":
        return 0
    source = payload.get("source", "startup")
    status_path = PROJECT / "backend" / "status.md"
    status = status_path.read_text(encoding="utf-8") if status_path.exists() else "(backend/status.md does not exist yet)"
    if len(status) > MAX_STATUS:
        status = status[:MAX_STATUS] + "\n...(truncated: read backend/status.md)"
    dirty = "\n".join(git("status", "--short").splitlines()[:30]) or "(clean)"
    ctx = (
        f"Axelrod backend build, session {source}. If you are the main session you are the orchestrator: "
        "follow backend/RUNBOOK.md and keep backend/status.md current. Subagents follow their role card "
        "and their plan in backend/PLANS.md.\n\n"
        f"Board (backend/status.md):\n{status}\n\n"
        f"Last commits:\n{git('log', '--oneline', '-6') or '(none)'}\n\n"
        f"Uncommitted files:\n{dirty}\n"
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
