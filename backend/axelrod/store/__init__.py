"""Run persistence: Store keeps RunState as local JSON or in DynamoDB plus S3. RunState and Vault are re-exported.

Layout, the same in both modes (local root is DATA_DIR, aws root is the vault bucket):
    runs/<run_id>/state.json                          RunState without its events (outside the vault folder)
    runs/<run_id>/<slug>/00-Meta/events.jsonl         every event, one JSON object per line
AWS mode also writes each event to EVENTS_TABLE (run_id, seq) as it happens, and the Run row to RUNS_TABLE
whenever the state is saved. In-memory RunState is what the API serves; the stores are for restarts.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from ..cache import run_cache_mode
from ..config import BACKEND_DIR, Settings
from ..contract import m
from ..ids import new_run_id, now_iso
from .events import (DynamoEvents, append_event, dynamo_item_for, event_line, gapless, lock_for, parse_jsonl,
                     to_jsonl)
from .state import TERMINAL_STATUSES, RunState
from .vault import Vault, data_root, is_text, register_replay, register_run

__all__ = ["Store", "RunState", "Vault", "REPLAY_DIR", "INTERRUPTED"]

log = logging.getLogger(__name__)

REPLAY_DIR = BACKEND_DIR / "data" / "replay"
STATE_FILE = "state.json"
EVENTS_REL = "00-Meta/events.jsonl"
INTERRUPTED = "Interrupted: the API restarted while this run was in progress"
FAKE_MODELS_NOTE = "Simulated: model answers come from offline fakes (FAKE_MODELS=1)"
FLUSH_DELAY_S = 0.2


class Store:
    def __init__(self, settings: Settings, *, s3: Any = None, dynamodb: Any = None,
                 replay_dir: Path | None = None) -> None:
        self.settings = settings
        self.replay_dir = replay_dir or REPLAY_DIR
        self.vault = Vault(settings, s3=s3)
        self._ddb = dynamodb
        self._runs: dict[str, RunState] = {}
        self._pending: dict[str, list[dict]] = {}
        self._flushers: dict[str, asyncio.Task] = {}
        self._secrets: dict[str, str] = {}
        self._lock = threading.Lock()

    # -- runs in memory -------------------------------------------------------------------

    @property
    def aws(self) -> bool:
        return self.settings.store == "aws"

    def create_run(self, req: m.CreateRunRequest) -> RunState:
        """A fresh live run at stage plan. Not persisted yet: the caller emits run.created, then saves.

        With FAKE_MODELS=1 the notes say the run is simulated. mode stays "live", the only fitting contract value.
        """
        now = now_iso()
        run_id = self.fresh_run_id()
        budget = req.budget_usd if req.budget_usd is not None else self.settings.budget_usd
        notes = req.notes
        if self.settings.fake_models:
            notes = f"{notes.strip()} | {FAKE_MODELS_NOTE}" if (notes or "").strip() else FAKE_MODELS_NOTE
        run = m.Run(run_id=run_id, company=req.company.strip(), ticker=(req.ticker or "").strip() or None,
                    depth=req.depth, status="running", stage="plan", created_at=now, updated_at=now,
                    created_via=req.created_via, notes=notes, policy_version=0, counts=m.RunCounts(),
                    usage=m.Usage(budget_usd=budget), last_seq=0, mode="live",
                    cache_mode=run_cache_mode(self.settings))
        state = RunState(run=run)
        register_run(run_id, run.company)
        self._runs[run_id] = state
        return state

    def fresh_run_id(self) -> str:
        """A new run id no run in this store has (a fork needs one too)."""
        run_id = new_run_id()
        while run_id in self._runs:
            run_id = new_run_id()
        return run_id

    def get(self, run_id: str) -> RunState:
        """Raises KeyError for an unknown run (the API turns it into 404)."""
        return self._runs[run_id]

    def list_runs(self) -> list[m.Run]:
        """Newest first."""
        return [s.run for s in sorted(self._runs.values(), key=lambda s: s.run.created_at, reverse=True)]

    def put(self, state: RunState) -> None:
        """Adopt a state built elsewhere (a replay, a test)."""
        register_run(state.run.run_id, state.run.company)
        self._runs[state.run.run_id] = state

    # -- secrets (AddSourceRequest.api_key): memory only, never written anywhere ---------

    def put_secret(self, ref: str, value: str) -> None:
        self._secrets[ref] = value

    def get_secret(self, ref: str) -> str | None:
        return self._secrets.get(ref)

    # -- events -----------------------------------------------------------------------------

    def append(self, state: RunState, type: str, data: dict | Any, *, message: str, stage: str | None = None,
               agent_id: str | None = None, level: str = "info") -> m.Event:
        """append_event plus persistence, under the run's lock (RunContext.emit uses this)."""
        with lock_for(state.run.run_id):
            event = append_event(state, type, data, message=message, stage=stage, agent_id=agent_id, level=level)
            self.on_event(state, event)
            return event

    def on_event(self, state: RunState, event: m.Event) -> None:
        """Persist one event: a line in the local events.jsonl, or a queued DynamoDB write."""
        if not self.aws:
            path = data_root(self.settings) / self.vault.root(state.run.run_id) / EVENTS_REL
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(event_line(event) + "\n")
            return
        run_id = state.run.run_id
        with self._lock:
            self._pending.setdefault(run_id, []).append(dynamo_item_for(event))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:            # called from a worker thread or a script: write now
            self._write_pending_sync(run_id)
            return
        task = self._flushers.get(run_id)
        if task is None or task.done():
            self._flushers[run_id] = loop.create_task(self._flush_loop(run_id, FLUSH_DELAY_S))

    def _take_pending(self, run_id: str) -> list[dict]:
        with self._lock:
            return self._pending.pop(run_id, [])

    def _write_pending_sync(self, run_id: str) -> None:
        batch = self._take_pending(run_id)
        if batch:
            self.events_table().put_many(batch)

    async def _flush_loop(self, run_id: str, delay: float) -> None:
        """Batch the event writes so the event loop never waits on DynamoDB."""
        if delay:
            await asyncio.sleep(delay)
        while batch := self._take_pending(run_id):
            try:
                await asyncio.to_thread(self.events_table().put_many, batch)
            except Exception:
                log.exception("DynamoDB event write failed for %s (S3 events.jsonl still has them)", run_id)

    async def flush_events(self, run_id: str | None = None) -> None:
        """Write every queued DynamoDB event now (one run, or all)."""
        if not self.aws:
            return
        for rid in [run_id] if run_id else list(self._pending):
            await self._flush_loop(rid, 0)

    # -- aws clients ----------------------------------------------------------------------

    def dynamodb(self) -> Any:
        if self._ddb is None:
            import boto3

            self._ddb = boto3.client("dynamodb", region_name=self.settings.aws_region)
        return self._ddb

    def events_table(self) -> DynamoEvents:
        if not self.settings.events_table:
            raise RuntimeError("EVENTS_TABLE is not set (STORE=aws needs it)")
        return DynamoEvents(self.dynamodb(), self.settings.events_table)

    def _runs_table(self) -> str:
        if not self.settings.runs_table:
            raise RuntimeError("RUNS_TABLE is not set (STORE=aws needs it)")
        return self.settings.runs_table

    # -- saving -----------------------------------------------------------------------------

    def save(self, state: RunState) -> None:
        """Snapshot the state now. Sync: in aws mode this calls S3 and DynamoDB in the calling thread."""
        snapshot = self._snapshot(state)
        if not self.aws:
            self._write_local_state(state.run.run_id, snapshot)
            return
        with lock_for(state.run.run_id):
            events_text = to_jsonl(state.events)
            run = state.run.model_copy(deep=True)
        self._write_pending_sync(state.run.run_id)
        self._write_aws(state.run.run_id, snapshot, events_text, run)

    async def save_async(self, state: RunState) -> None:
        """save() for the pipeline: serialize on the loop (no torn reads), upload in a thread, never raise."""
        try:
            snapshot = self._snapshot(state)
            if not self.aws:
                self._write_local_state(state.run.run_id, snapshot)
                return
            with lock_for(state.run.run_id):
                events_text = to_jsonl(state.events)
                run = state.run.model_copy(deep=True)
            await self.flush_events(state.run.run_id)
            await asyncio.to_thread(self._write_aws, state.run.run_id, snapshot, events_text, run)
        except Exception:
            log.exception("saving %s failed", state.run.run_id)

    @staticmethod
    def _snapshot(state: RunState) -> str:
        with lock_for(state.run.run_id):
            return state.model_dump_json(exclude={"events"})

    def _write_local_state(self, run_id: str, snapshot: str) -> None:
        target = data_root(self.settings) / "runs" / run_id / STATE_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(STATE_FILE + ".tmp")
        tmp.write_text(snapshot, encoding="utf-8")
        tmp.replace(target)

    def _write_aws(self, run_id: str, snapshot: str, events_text: str, run: m.Run) -> None:
        s3 = self.vault.s3()
        s3.put_object(Bucket=self.vault.bucket, Key=f"runs/{run_id}/{STATE_FILE}", Body=snapshot.encode("utf-8"),
                      ContentType="application/json")
        self.vault.put_text(run_id, EVENTS_REL, events_text)
        self.dynamodb().put_item(TableName=self._runs_table(), Item={
            "run_id": {"S": run_id}, "created_at": {"S": run.created_at}, "status": {"S": run.status},
            "company": {"S": run.company}, "mode": {"S": run.mode}, "run": {"S": run.model_dump_json()}})

    async def close(self) -> None:
        """Flush queued event writes (API shutdown)."""
        await self.flush_events()

    # -- loading ----------------------------------------------------------------------------

    def load_all(self) -> None:
        """At startup: every stored run, plus backend/data/replay/* as mode "replay".
        A run that was still in progress when the API died is marked failed, since nothing resumes it."""
        loaded = self._load_aws() if self.aws else self._load_local()
        for state in loaded:
            self._adopt(state)
        for state in self._load_replays():
            if state.run.run_id in self._runs:
                log.warning("replay %s skipped: a stored run has the same id", state.run.run_id)
                continue
            self.put(state)

    def _adopt(self, state: RunState) -> None:
        self.put(state)
        state.name_agents()             # snapshots from before v0.3 have unnamed agents
        state.recount()
        state.recount_gateway()
        if state.events:
            state.run.last_seq = state.events[-1].seq
            state.run.updated_at = max(state.run.updated_at, state.events[-1].ts)
        if state.run.mode != "live":
            return
        changed = False
        if state.run.status not in TERMINAL_STATUSES:
            stage = state.run.stage
            self.append(state, "run.status", {"status": "failed", "stage": stage}, stage=stage, level="error",
                        message=f"Run failed at {stage}: {INTERRUPTED}")
            self.append(state, "run.failed", {"error": INTERRUPTED}, stage=stage, level="error",
                        message=f"Run failed: {INTERRUPTED}")
            changed = True
        changed = self._settle_agents(state) or changed
        if changed:
            self.save(state)

    def _settle_agents(self, state: RunState) -> bool:
        """Agents left queued or working by a process that died get the run's end status, so the swarm settles."""
        final = "stopped" if state.run.status == "stopped" else "failed"
        busy = [a for a in state.agents.values() if a.status in ("queued", "working")]
        for agent in busy:
            agent.status, agent.finished_at = final, now_iso()
            self.append(state, "agent.status", {"agent_id": agent.agent_id, "status": final, "task": agent.task,
                                                "usage": agent.usage}, agent_id=agent.agent_id, level="warn",
                        message=f"{agent.agent_id} {final}: the API restarted")
        return bool(busy)

    def _load_local(self) -> list[RunState]:
        out = []
        base = data_root(self.settings) / "runs"
        for folder in sorted(base.iterdir()) if base.is_dir() else []:
            f = folder / STATE_FILE
            if not f.is_file():
                continue
            try:
                state = RunState.model_validate_json(f.read_text(encoding="utf-8"))
                root = register_run(state.run.run_id, state.run.company)
                events_file = data_root(self.settings) / root / EVENTS_REL
                state.events = parse_jsonl(events_file.read_text(encoding="utf-8")) if events_file.is_file() else []
                out.append(state)
            except Exception:
                log.exception("skipping unreadable run folder %s", folder)
        return out

    def _load_aws(self) -> list[RunState]:
        out = []
        for run_id in self._scan_run_ids():
            try:
                body = self.vault.s3().get_object(Bucket=self.vault.bucket, Key=f"runs/{run_id}/{STATE_FILE}")
                state = RunState.model_validate_json(body["Body"].read())
                register_run(run_id, state.run.company)
                ddb_events, oversize = self.events_table().query(run_id)
                s3_events = self._s3_events(run_id)
                state.events = ddb_events if gapless(ddb_events) and not oversize and \
                    len(ddb_events) >= len(s3_events) else s3_events
                out.append(state)
            except Exception:
                log.exception("skipping run %s: could not load it from S3/DynamoDB", run_id)
        return out

    def _scan_run_ids(self) -> list[str]:
        ids: list[str] = []
        kwargs: dict[str, Any] = {"TableName": self._runs_table(), "ProjectionExpression": "run_id"}
        while True:
            resp = self.dynamodb().scan(**kwargs)
            ids += [it["run_id"]["S"] for it in resp.get("Items", [])]
            if not resp.get("LastEvaluatedKey"):
                return ids
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def _s3_events(self, run_id: str) -> list[m.Event]:
        try:
            return parse_jsonl(self.vault.get_text(run_id, EVENTS_REL))
        except KeyError:
            return []

    def _load_replays(self) -> list[RunState]:
        """backend/data/replay/<name>/{state.json, events.jsonl, vault/}: recorded real runs, labeled replay."""
        out = []
        base = Path(self.replay_dir)
        for folder in sorted(base.iterdir()) if base.is_dir() else []:
            f = folder / STATE_FILE
            if not f.is_file():
                continue
            try:
                state = RunState.model_validate_json(f.read_text(encoding="utf-8"))
                events_file = folder / "events.jsonl"
                state.events = parse_jsonl(events_file.read_text(encoding="utf-8")) if events_file.is_file() else []
                state.run.mode = "replay"
                state.name_agents()
                if state.events:
                    state.run.last_seq = state.events[-1].seq
                state.recount()
                state.recount_gateway()
                register_replay(state.run.run_id, folder / "vault")
                out.append(state)
            except Exception:
                log.exception("skipping unreadable replay folder %s", folder)
        return out

    # -- export -----------------------------------------------------------------------------

    def export_run(self, run_id: str, dest: Path) -> None:
        """Write a finished run (state, events, page texts, never PDFs) to dest, in the replay folder format."""
        state = self.get(run_id)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / STATE_FILE).write_text(self._snapshot(state), encoding="utf-8")
        with lock_for(run_id):
            events_text = to_jsonl(state.events)
        (dest / "events.jsonl").write_text(events_text, encoding="utf-8", newline="\n")
        for rel in self.vault.list_files(run_id):
            if not is_text(rel):
                continue                                   # raw PDFs and other binaries stay out of the repo
            target = dest / "vault" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.vault.get_bytes(run_id, rel))
