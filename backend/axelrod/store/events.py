"""Event log: gapless seq per run, the GET /events?after&limit paging rules (CONTRACT.md section 4),
and the DynamoDB event writes in aws mode.

`append_event` is the one place a seq is assigned. It runs under a per-run lock, so emits from
asyncio tasks and from worker threads both come out gapless and ordered.
"""
from __future__ import annotations

import bisect
import threading
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, TypeAdapter
from pydantic_core import to_jsonable_python

from ..contract import m
from ..ids import now_iso

if TYPE_CHECKING:
    from .state import RunState

EVENT_ADAPTER: TypeAdapter = TypeAdapter(m.Event)
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000
DYNAMO_BODY_MAX = 350_000          # DynamoDB items cap at 400 KB; bigger events live only in S3 events.jsonl

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(run_id: str) -> threading.RLock:
    """One lock per run, shared by every RunContext for that run."""
    with _locks_guard:
        return _locks.setdefault(run_id, threading.RLock())


# ---------------------------------------------------------------------------
# Building and folding
# ---------------------------------------------------------------------------


def build_event(state: RunState, type: str, data: dict | BaseModel, *, message: str, stage: str | None,
                agent_id: str | None, level: str, seq: int) -> m.Event:
    """Validate one event against the contract union. Data is dumped to JSON first, so the event is a snapshot
    that later changes to the state's objects can't reach."""
    if type not in m.EVENT_TYPES:
        raise ValueError(f"unknown event type {type!r}")
    raw = {"seq": seq, "ts": now_iso(), "run_id": state.run.run_id, "type": type,
           "stage": stage or state.run.stage, "agent_id": agent_id, "level": level, "message": message,
           "data": to_jsonable_python(data)}
    return EVENT_ADAPTER.validate_python(raw)


def fold(state: RunState, event: m.Event) -> None:
    """Keep the run-level fields in sync with what was emitted. Items, agents and sources are not folded:
    the stages own those objects and change them before they emit."""
    run, d = state.run, event.data
    t = event.type
    if t == "run.status":
        run.status, run.stage = d.status, d.stage
    elif t == "run.stopped":
        run.status = "stopped"
        run.stop_reason = d.reason or run.stop_reason
    elif t == "run.failed":
        run.status, run.error = "failed", d.error
    elif t == "run.completed":
        run.status, run.stage = "done", "complete"
    elif t == "plan.ready":
        state.plan_ready = True
    elif t == "policy.updated":
        run.policy_version = max(run.policy_version, d.version)
    elif t == "gateway.request":
        name = f"gateway_{d.decision}"
        setattr(run.counts, name, getattr(run.counts, name) + 1)
    elif t == "usage.updated":
        _copy_usage(run.usage, d.usage)
    elif t == "report.ready":
        state.report_ready = True
    elif t == "summary.ready":
        state.summary_ready = True
    run.last_seq = event.seq
    run.updated_at = event.ts
    state.recount()


def _copy_usage(target: m.Usage, src: m.Usage) -> None:
    """In place, so code holding a reference to run.usage keeps seeing the live object."""
    target.model_calls, target.tokens_in, target.tokens_out = src.model_calls, src.tokens_in, src.tokens_out
    target.cost_usd = src.cost_usd
    if src.budget_usd is not None:
        target.budget_usd = src.budget_usd


def append_event(state: RunState, type: str, data: dict | BaseModel, *, message: str, stage: str | None = None,
                 agent_id: str | None = None, level: str = "info") -> m.Event:
    """Build, append and fold under the run's lock. seq is always len(events) + 1."""
    with lock_for(state.run.run_id):
        event = build_event(state, type, data, message=message, stage=stage, agent_id=agent_id, level=level,
                            seq=len(state.events) + 1)
        state.events.append(event)
        fold(state, event)
        return event


# ---------------------------------------------------------------------------
# Paging (GET /events)
# ---------------------------------------------------------------------------


def events_page(state: RunState, after: int = 0, limit: int = DEFAULT_LIMIT) -> m.EventsPage:
    """Events with seq > after, at most limit (default 500, max 2000), plus last_seq, run_status and stage.
    With nothing new, last_seq echoes `after`, the way the mock does."""
    after = max(after, 0)
    limit = min(max(limit, 1), MAX_LIMIT)
    with lock_for(state.run.run_id):
        start = bisect.bisect_right(state.events, after, key=lambda e: e.seq)
        evs = state.events[start:start + limit]
        return m.EventsPage(run_id=state.run.run_id, events=evs, last_seq=evs[-1].seq if evs else after,
                            run_status=state.run.status, stage=state.run.stage)


# ---------------------------------------------------------------------------
# Serialization (events.jsonl, replays)
# ---------------------------------------------------------------------------


def event_line(event: m.Event) -> str:
    return event.model_dump_json()


def to_jsonl(events: Iterable[m.Event]) -> str:
    return "".join(event_line(e) + "\n" for e in events)


def parse_jsonl(text: str) -> list[m.Event]:
    """Events from a jsonl file. Stops at the first broken line (a write cut short by a crash)."""
    out: list[m.Event] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            out.append(EVENT_ADAPTER.validate_json(line))
        except ValueError:
            break
    return out


def gapless(events: list[m.Event]) -> bool:
    return [e.seq for e in events] == list(range(1, len(events) + 1))


# ---------------------------------------------------------------------------
# DynamoDB (aws mode): table EVENTS_TABLE, partition key run_id (S), sort key seq (N)
# ---------------------------------------------------------------------------


class DynamoEvents:
    """Thin wrapper over the low-level DynamoDB client. Bodies are stored as JSON strings, so floats and
    nested shapes never meet DynamoDB's number rules."""

    def __init__(self, client: Any, table: str) -> None:
        self.client = client
        self.table = table

    @staticmethod
    def item(run_id: str, seq: int, type: str, ts: str, body: str) -> dict:
        item = {"run_id": {"S": run_id}, "seq": {"N": str(seq)}, "type": {"S": type}, "ts": {"S": ts}}
        if len(body.encode("utf-8")) <= DYNAMO_BODY_MAX:
            item["body"] = {"S": body}
        else:
            item["oversize"] = {"BOOL": True}
        return item

    def put_many(self, items: list[dict]) -> None:
        """batch_write_item in chunks of 25, retrying unprocessed items with a short backoff."""
        for i in range(0, len(items), 25):
            requests = [{"PutRequest": {"Item": it}} for it in items[i:i + 25]]
            for attempt in range(8):
                resp = self.client.batch_write_item(RequestItems={self.table: requests})
                requests = (resp.get("UnprocessedItems") or {}).get(self.table) or []
                if not requests:
                    break
                time.sleep(0.05 * 2 ** attempt)
            if requests:
                raise RuntimeError(f"DynamoDB left {len(requests)} event writes unprocessed")

    def query(self, run_id: str) -> tuple[list[m.Event], bool]:
        """Every stored event for a run in seq order, and whether any body was too big to keep in DynamoDB."""
        events: list[m.Event] = []
        oversize = False
        kwargs: dict[str, Any] = {"TableName": self.table, "KeyConditionExpression": "run_id = :r",
                                  "ExpressionAttributeValues": {":r": {"S": run_id}}, "ConsistentRead": True}
        while True:
            resp = self.client.query(**kwargs)
            for it in resp.get("Items", []):
                if "body" in it:
                    events.append(EVENT_ADAPTER.validate_json(it["body"]["S"]))
                else:
                    oversize = True
            if not resp.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        events.sort(key=lambda e: e.seq)
        return events, oversize


def dynamo_item_for(event: m.Event) -> dict:
    return DynamoEvents.item(event.run_id, event.seq, event.type, event.ts, event_line(event))
