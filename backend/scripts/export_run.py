"""Export a finished run (state, events, page texts, never PDFs) to backend/data/replay/<name>/ for the replay fallback.

Usage (from the repo root): PY backend/scripts/export_run.py <run_id> [--name NAME] [--dest DIR]

Reads the run from the store the settings point at (STORE=local: DATA_DIR, STORE=aws: S3 and DynamoDB, read
only) and writes it with Store.export_run in the replay format: state.json, events.jsonl and vault/ (text
files only). The API loads every folder there at startup as mode "replay", which the UI labels as recorded.
It refuses runs still in progress, replays, and a destination that already exists.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports

MAX_MB = 25          # a replay lives in the git repo: warn when it gets big


def _load(store, run_id: str):
    """The stored runs, without the side effects of Store.load_all (which marks unfinished live runs failed
    and saves them, and must never touch a run that the deployed API is still running)."""
    loaded = store._load_aws() if store.aws else store._load_local()
    for state in loaded:
        if state.run.run_id == run_id:
            return state
    return None


def _folder_mb(folder: Path) -> float:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()) / 1_000_000


def check_export(dest: Path) -> tuple[int, int, list[str]]:
    """(events, vault files, problems) for an exported folder: it must load the way the API loads replays."""
    from backend.axelrod.store import RunState
    from backend.axelrod.store.events import gapless, parse_jsonl
    from backend.axelrod.store.vault import is_text

    problems: list[str] = []
    state = RunState.model_validate_json((dest / "state.json").read_text(encoding="utf-8"))
    events = parse_jsonl((dest / "events.jsonl").read_text(encoding="utf-8"))
    if not gapless(events):
        problems.append("events.jsonl is not gapless from seq 1")
    if events and state.run.last_seq and events[-1].seq < state.run.last_seq:
        problems.append(f"events end at seq {events[-1].seq}, but the run says last_seq {state.run.last_seq}")
    files = [p for p in (dest / "vault").rglob("*") if p.is_file()] if (dest / "vault").is_dir() else []
    binary = [str(p.relative_to(dest)) for p in files if not is_text(p.name)]
    if binary:
        problems.append(f"binary files in the export: {', '.join(binary[:5])}")
    return len(events), len(files), problems


def main(argv: list[str] | None = None) -> int:
    from backend.axelrod.config import get_settings
    from backend.axelrod.store import REPLAY_DIR, Store
    from backend.axelrod.store.state import TERMINAL_STATUSES

    ap = argparse.ArgumentParser(description="Export a finished run to backend/data/replay/<name>/ (text only).")
    ap.add_argument("run_id")
    ap.add_argument("--name", help="folder name under backend/data/replay (default: the run id)")
    ap.add_argument("--dest", type=Path, help="write here instead of backend/data/replay/<name>")
    args = ap.parse_args(argv)

    store = Store(get_settings())
    state = _load(store, args.run_id)
    if state is None:
        print(f"No stored run {args.run_id} (STORE={store.settings.store}).", file=sys.stderr)
        return 1
    if state.run.mode != "live":
        print(f"{args.run_id} is a {state.run.mode} run. Only live runs are exported.", file=sys.stderr)
        return 1
    if state.run.status not in TERMINAL_STATUSES:
        print(f"{args.run_id} is still {state.run.status} at {state.run.stage}. Export it once it is finished.",
              file=sys.stderr)
        return 1
    dest = args.dest or (REPLAY_DIR / (args.name or args.run_id))
    if dest.exists() and any(dest.iterdir()):
        print(f"{dest} already exists. Pick another --name, or remove it first.", file=sys.stderr)
        return 1

    store.put(state)
    store.export_run(args.run_id, dest)
    n_events, n_files, problems = check_export(dest)
    print(f"Exported {args.run_id} ({state.run.company}, {state.run.status}) to {dest}")
    print(f"  {n_events} events, {len(state.items)} items, {len(state.documents)} documents, "
          f"{n_files} vault text files, {_folder_mb(dest):.1f} MB. PDFs and other binaries are left out.")
    if state.run.status != "done":
        print(f"  Note: the run ended {state.run.status}, so the replay shows it {state.run.status}.")
    if _folder_mb(dest) > MAX_MB:
        print(f"  Warning: over {MAX_MB} MB. That is a lot for a git repo.")
    for p in problems:
        print(f"  Problem: {p}", file=sys.stderr)
    print("  The API loads it at startup as mode replay (labeled as recorded in the UI).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
