"""DELETE /runs/{run_id} (v0.3.1, B-delete-pace): a finished run's own data goes, the shared caches stay.

Offline. The API tests run tiny callable stages (no gateway, no models). The S3 and DynamoDB path runs against
small in-memory fakes of the boto3 clients, which also record every call so the batches, the prefixes and the
fail-safe order can be checked. DATA_DIR is a short folder under backend/.data (Windows MAX_PATH)."""
from __future__ import annotations

import asyncio
import secrets
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.axelrod import orchestrator, rundelete
from backend.axelrod.api import create_app
from backend.axelrod.config import BACKEND_DIR, Settings, load_settings
from backend.axelrod.contract import m
from backend.axelrod.runtime import RunContext
from backend.axelrod.store import Store
from backend.axelrod.store.events import lock_for

P = "/api/v1"
SHL = {"company": "Siemens Healthineers AG", "ticker": "SHL", "created_via": "api"}
CACHE_FILES = ("cache/docs/abc.bin", "cache/docs/abc.json", "cache/models/k1.json", "archive/shl/manifest.json")
NOTE = "Run data removed. The shared document cache and recorded model answers stay."


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def short_settings(settings: Settings) -> Settings:
    """The offline settings with DATA_DIR at backend/.data/dl<hex>, removed afterwards."""
    data_dir = BACKEND_DIR / ".data" / f"dl{secrets.token_hex(3)}"
    data_dir.mkdir(parents=True)
    yield settings.model_copy(update={"data_dir": data_dir})
    shutil.rmtree(data_dir, ignore_errors=True)


def put_caches(data_dir: Path) -> dict[str, bytes]:
    """Fake shared cache entries in DATA_DIR (simulated): they must survive every delete."""
    out = {}
    for rel in CACHE_FILES:
        f = data_dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        out[rel] = f"simulated cache entry {rel}".encode()
        f.write_bytes(out[rel])
    return out


def assert_caches(data_dir: Path, caches: dict[str, bytes]) -> None:
    for rel, body in caches.items():
        assert (data_dir / rel).read_bytes() == body, rel


async def finish(ctx: RunContext) -> None:
    """A stage that leaves a vault file behind, then ends."""
    ctx.vault.put_text(ctx.run_id, "00-Meta/notes.md", "simulated stage output")
    ctx.vault.put_text(ctx.run_id, "01-Annual-Reports/page.txt", "simulated page text")


async def fail(ctx: RunContext) -> None:
    ctx.vault.put_text(ctx.run_id, "00-Meta/notes.md", "simulated stage output")
    raise RuntimeError("simulated failure")


async def hold(ctx: RunContext) -> None:
    """Keeps the run running until the kill switch."""
    ctx.vault.put_text(ctx.run_id, "00-Meta/notes.md", "simulated stage output")
    while True:
        ctx.check_stop()
        await asyncio.sleep(0.01)


@pytest.fixture
def stages(monkeypatch):
    def use(fn):
        monkeypatch.setattr(orchestrator, "STAGES", [("collect", fn)])
    return use


def wait(client: TestClient, run_id: str, pred, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    run: dict = {}
    while time.monotonic() < deadline:
        run = client.get(f"{P}/runs/{run_id}").json()
        if pred(run):
            return run
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting, last run: {run}")


def new_run(client: TestClient) -> str:
    r = client.post(f"{P}/runs", json=SHL)
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def listed(client: TestClient) -> list[str]:
    return [r.run_id for r in m.RunList.model_validate(client.get(f"{P}/runs").json()).runs]


def assert_error(r, status: int, code: str, text: str) -> None:
    assert r.status_code == status, r.text
    body = m.ErrorResponse.model_validate(r.json()).error
    assert body.code == code and text in body.message, r.text


# ---------------------------------------------------------------------------
# API, local store
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["done", "stopped", "failed"])
def test_delete_a_finished_run(short_settings, stages, kind):
    data_dir = Path(short_settings.data_dir)
    caches = put_caches(data_dir)
    stages({"done": finish, "stopped": hold, "failed": fail}[kind])
    with TestClient(create_app(short_settings)) as client:
        run_id = new_run(client)
        keep = new_run(client)                                  # a second run that must not be touched
        if kind == "stopped":
            for rid in (run_id, keep):
                wait(client, rid, lambda r: r["stage"] == "collect")
                assert client.post(f"{P}/runs/{rid}/stop", json={}).status_code == 200
        for rid in (run_id, keep):
            wait(client, rid, lambda r: r["status"] == kind)
        base = data_dir / "runs" / run_id
        vault = base / "siemens-healthineers"
        assert (base / "state.json").is_file() and (vault / "00-Meta" / "notes.md").is_file()
        assert (vault / "00-Meta" / "events.jsonl").is_file()
        n_events = client.get(f"{P}/runs/{run_id}").json()["last_seq"]
        time.sleep(0.1)                                         # let the pipeline task write its last save
        n_files = sum(1 for p in vault.rglob("*") if p.is_file())

        r = client.delete(f"{P}/runs/{run_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"run_id", "events", "vault_files", "note"}
        m.RunDeleted.model_validate(body)                       # contract 0.3.1 shape
        assert body["run_id"] == run_id and body["note"] == NOTE
        assert body["events"] >= n_events > 0                  # every row in events.jsonl
        assert body["vault_files"] == n_files >= (3 if kind == "done" else 2)   # events.jsonl plus the stage's
        assert not base.exists()
        assert run_id not in listed(client) and keep in listed(client)
        assert_error(client.get(f"{P}/runs/{run_id}"), 404, "not_found", run_id)
        assert_error(client.get(f"{P}/runs/{run_id}/events"), 404, "not_found", run_id)
        assert_error(client.delete(f"{P}/runs/{run_id}"), 404, "not_found", run_id)     # a retry: already gone
        assert (data_dir / "runs" / keep / "state.json").is_file()
        assert_caches(data_dir, caches)

    with TestClient(create_app(short_settings)) as client:         # a restart doesn't bring it back
        assert run_id not in listed(client) and keep in listed(client)
    assert_caches(data_dir, caches)


def _load_smoke():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("contract_smoke", BACKEND_DIR / "scripts" / "contract_smoke.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["contract_smoke"] = module
    spec.loader.exec_module(module)
    return module


def test_contract_smoke_delete_flow(short_settings, stages):
    """contract_smoke.delete_flow against the app in process (TestClient, no server): only its own run goes."""
    smoke_mod = _load_smoke()
    stages(hold)
    with TestClient(create_app(short_settings)) as client:
        other = new_run(client)
        wait(client, other, lambda r: r["stage"] == "collect")
        client.post(f"{P}/runs/{other}/stop", json={})

        class InProcess(smoke_mod.Smoke):
            def call(self, method, path, body=None, params=None, root=False):
                r = client.request(method, ("" if root else P) + path, json=body, params=params)
                return r.status_code, (r.json() if r.content else None)

        smoke = InProcess(P, None, 10)
        smoke.delete_flow(SHL["company"], SHL["ticker"], 10)
        assert smoke.failures == [], smoke.failures
        assert smoke.passed >= 6
        assert listed(client) == [other]                         # the run the smoke didn't create stays


def test_running_run_answers_409(short_settings, stages):
    stages(hold)
    with TestClient(create_app(short_settings)) as client:
        run_id = new_run(client)
        wait(client, run_id, lambda r: r["stage"] == "collect")
        assert_error(client.delete(f"{P}/runs/{run_id}"), 409, "invalid_state",
                     "Run is running. Stop it before deleting it")
        assert run_id in listed(client)
        assert (Path(short_settings.data_dir) / "runs" / run_id / "state.json").is_file()
        client.post(f"{P}/runs/{run_id}/stop", json={})
        wait(client, run_id, lambda r: r["status"] == "stopped")
        assert client.delete(f"{P}/runs/{run_id}").status_code == 200


# ---------------------------------------------------------------------------
# refusals, direct
# ---------------------------------------------------------------------------


def runtime(settings: Settings, store: Store) -> SimpleNamespace:
    """What rundelete needs from the API's Runtime."""
    return SimpleNamespace(settings=settings, store=store, vault=store.vault, contexts={})


def finished_state(store: Store, status: str = "done"):
    state = store.create_run(m.CreateRunRequest(**SHL))
    state.run.status = status
    store.vault.put_text(state.run.run_id, "00-Meta/notes.md", "simulated")
    store.save(state)
    return state


@pytest.mark.parametrize("status", ["running", "awaiting_approval"])
async def test_unfinished_run_is_refused(short_settings, status):
    store = Store(short_settings)
    state = finished_state(store, status)
    with pytest.raises(rundelete.CannotDelete, match=f"^Run is {status}. Stop it before deleting it$"):
        await rundelete.delete_run(runtime(short_settings, store), state)
    assert store.get(state.run.run_id) is state
    assert (Path(short_settings.data_dir) / "runs" / state.run.run_id / "state.json").is_file()


async def test_replay_is_refused(short_settings):
    store = Store(short_settings)
    state = finished_state(store)
    state.run.mode = "replay"
    with pytest.raises(rundelete.CannotDelete, match="^A recorded replay can't be deleted$"):
        await rundelete.delete_run(runtime(short_settings, store), state)
    assert store.get(state.run.run_id) is state


async def test_protected_run_is_refused(short_settings):
    store = Store(short_settings)
    state = finished_state(store)
    other = finished_state(store)
    rid = state.run.run_id
    protected = short_settings.model_copy(update={"protected_runs": [rid, "run_20261108_zzzzzz"]})
    with pytest.raises(rundelete.CannotDelete, match=f"^{rid} is protected \\(the demo run\\)$"):
        await rundelete.delete_run(runtime(protected, store), state)
    assert store.get(rid) is state
    out = await rundelete.delete_run(runtime(protected, store), other)
    assert out["run_id"] == other.run.run_id


def test_protected_runs_setting_parses_a_comma_list():
    s = load_settings(env_file=None, environ={"PROTECTED_RUNS": " run_20261108_aaaaaa, ,run_20261108_bbbbbb "})
    assert s.protected_runs == ["run_20261108_aaaaaa", "run_20261108_bbbbbb"]
    assert load_settings(env_file=None, environ={}).protected_runs == []


async def test_delete_cancels_side_tasks_and_drops_the_context(short_settings):
    store = Store(short_settings)
    state = finished_state(store)
    ctx = RunContext.create(state, store, store.vault, short_settings)
    started = asyncio.Event()

    async def chat_reply():
        started.set()
        await asyncio.sleep(30)
        ctx.vault.put_text(ctx.run_id, "00-Meta/late.md", "must never be written")

    task = ctx.spawn(chat_reply(), name="chat")
    await started.wait()
    rt = runtime(short_settings, store)
    rt.contexts[ctx.run_id] = ctx
    out = await rundelete.delete_run(rt, state)
    assert task.cancelled() and ctx.run_id not in rt.contexts and out["vault_files"] == 1
    ctx.emit("agent.log", {"text": "late"}, message="late")          # a late emit recreates nothing
    store.save(state)
    assert not (Path(short_settings.data_dir) / "runs" / ctx.run_id).exists()


# ---------------------------------------------------------------------------
# aws mode, fake S3 and DynamoDB (simulated)
# ---------------------------------------------------------------------------


class FakeS3:
    """list_objects_v2 (paged at 1000), delete_objects, put_object and get_object over a dict."""

    def __init__(self, log: list) -> None:
        self.objects: dict[str, bytes] = {}
        self.log = log
        self.deny: set[str] = set()                    # keys that answer AccessDenied

    def put_object(self, *, Bucket, Key, Body, **kw):
        self.objects[Key] = Body

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        body = self.objects[Key]
        return {"Body": SimpleNamespace(read=lambda: body)}

    def list_objects_v2(self, *, Bucket, Prefix="", ContinuationToken=None, Delimiter=None, **kw):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + 1000]
        resp = {"Contents": [{"Key": k, "Size": len(self.objects[k])} for k in page],
                "IsTruncated": start + 1000 < len(keys)}
        if resp["IsTruncated"]:
            resp["NextContinuationToken"] = str(start + 1000)
        return resp

    def delete_objects(self, *, Bucket, Delete):
        keys = [o["Key"] for o in Delete["Objects"]]
        assert 1 <= len(keys) <= 1000
        self.log.append(("s3.delete", keys))
        errors = [{"Key": k, "Code": "AccessDenied"} for k in keys if k in self.deny]
        for k in keys:
            if k not in self.deny:
                self.objects.pop(k, None)
        return {"Errors": errors} if errors else {}


class FakeDynamo:
    """query (paged at 40), batch_write_item (one unprocessed delete on the first call), put_item, scan."""

    def __init__(self, log: list) -> None:
        self.tables: dict[str, dict[tuple, dict]] = {"runs-t": {}, "events-t": {}}
        self.log = log
        self.throttle_once = True

    @staticmethod
    def _key(table: str, item: dict) -> tuple:
        return (item["run_id"]["S"], item["seq"]["N"]) if table == "events-t" else (item["run_id"]["S"],)

    def put_item(self, *, TableName, Item):
        self.tables[TableName][self._key(TableName, Item)] = Item

    def batch_write_item(self, *, RequestItems):
        unprocessed = {}
        for table, reqs in RequestItems.items():
            assert len(reqs) <= 25
            self.log.append(("ddb.batch", table, len(reqs)))
            left = []
            for i, req in enumerate(reqs):
                if "DeleteRequest" in req and self.throttle_once and i == len(reqs) - 1:
                    self.throttle_once = False
                    left.append(req)
                    continue
                if "PutRequest" in req:
                    self.put_item(TableName=table, Item=req["PutRequest"]["Item"])
                else:
                    self.tables[table].pop(self._key(table, req["DeleteRequest"]["Key"]), None)
            if left:
                unprocessed[table] = left
        return {"UnprocessedItems": unprocessed}

    def query(self, *, TableName, ExpressionAttributeValues, ExclusiveStartKey=None, **kw):
        rid = ExpressionAttributeValues[":r"]["S"]
        rows = sorted((it for k, it in self.tables[TableName].items() if k[0] == rid), key=lambda it: int(it["seq"]["N"]))
        start = int(ExclusiveStartKey["n"]) if ExclusiveStartKey else 0
        resp = {"Items": rows[start:start + 40]}
        if start + 40 < len(rows):
            resp["LastEvaluatedKey"] = {"n": start + 40}
        return resp

    def scan(self, *, TableName, **kw):
        return {"Items": [{"run_id": it["run_id"]} for it in self.tables[TableName].values()]}


def aws_store(settings: Settings):
    log: list = []
    s3, ddb = FakeS3(log), FakeDynamo(log)
    aws = settings.model_copy(update={"store": "aws", "vault_bucket": "vault-b", "runs_table": "runs-t",
                                      "events_table": "events-t"})
    return aws, Store(aws, s3=s3, dynamodb=ddb), s3, ddb, log


def seed_aws(store: Store, s3: FakeS3, n_files: int, n_events: int) -> tuple:
    """A finished run with n_files vault files and n_events events, plus another run and the shared caches."""
    state = store.create_run(m.CreateRunRequest(**SHL))
    other = store.create_run(m.CreateRunRequest(**SHL))
    for st in (state, other):
        for i in range(n_events):
            store.append(st, "agent.log", {"text": f"line {i}"}, message=f"line {i}")
        st.run.status = "done"
        store.save(st)
    rid = state.run.run_id
    for i in range(n_files - 1):                                   # events.jsonl is the last vault file
        store.vault.put_text(rid, f"01-Annual-Reports/p{i:04d}.txt", "simulated")
    store.vault.put_text(other.run.run_id, "00-Meta/notes.md", "simulated")
    for k in CACHE_FILES:
        s3.objects[k] = b"simulated cache entry"
    return state, other


async def test_aws_delete_batches_and_prefix(short_settings):
    aws, store, s3, ddb, log = aws_store(short_settings)
    state, other = seed_aws(store, s3, n_files=2300, n_events=60)
    rid = state.run.run_id
    n_events = len(state.events)
    assert sum(k[0] == rid for k in ddb.tables["events-t"]) == n_events
    log.clear()

    out = await rundelete.delete_run(runtime(aws, store), state)
    assert out == {"run_id": rid, "events": n_events, "vault_files": 2300, "note": NOTE}

    deletes = [e[1] for e in log if e[0] == "s3.delete"]
    assert [len(d) for d in deletes] == [1000, 1000, 300, 1]                 # batches, then state.json alone
    assert deletes[-1] == [f"runs/{rid}/state.json"]
    assert all(k.startswith(f"runs/{rid}/") for d in deletes for k in d)
    assert not any(k.startswith(("cache/", "archive/")) for d in deletes for k in d)
    assert all(k in s3.objects for k in CACHE_FILES)
    assert not any(k.startswith(f"runs/{rid}/") for k in s3.objects)
    assert any(k.startswith(f"runs/{other.run.run_id}/") for k in s3.objects)

    order = [e[0] if e[0] == "s3.delete" else f"ddb.{e[1]}" for e in log]
    first_event_delete = order.index("ddb.events-t")
    assert order.index("s3.delete") < first_event_delete              # vault first
    assert first_event_delete < len(order) - 1 - order[::-1].index("s3.delete")   # state.json after the events
    assert order[-1] == "ddb.runs-t"                                   # the runs row last
    assert all(e[2] <= 25 for e in log if e[0] == "ddb.batch")
    assert not any(k[0] == rid for k in ddb.tables["events-t"])
    assert sum(k[0] == other.run.run_id for k in ddb.tables["events-t"]) == len(other.events)
    assert (rid,) not in ddb.tables["runs-t"] and (other.run.run_id,) in ddb.tables["runs-t"]
    with pytest.raises(KeyError):
        store.get(rid)

    store.append(state, "agent.log", {"text": "late"}, message="late")   # late writes go nowhere
    await store.flush_events()
    store.save(state)
    assert not any(k[0] == rid for k in ddb.tables["events-t"])
    assert not any(k.startswith(f"runs/{rid}/") for k in s3.objects)


async def test_aws_delete_that_fails_half_way_can_be_retried(short_settings):
    aws, store, s3, ddb, log = aws_store(short_settings)
    state, _ = seed_aws(store, s3, n_files=5, n_events=3)
    rid = state.run.run_id
    blocked = next(k for k in s3.objects if k.startswith(f"runs/{rid}/siemens-healthineers/"))
    s3.deny.add(blocked)                                               # no s3:DeleteObject on one key
    with pytest.raises(RuntimeError, match="AccessDenied"):
        await rundelete.delete_run(runtime(aws, store), state)
    assert store.get(rid) is state                                     # still listed, record kept
    assert f"runs/{rid}/state.json" in s3.objects and (rid,) in ddb.tables["runs-t"]
    assert any(k[0] == rid for k in ddb.tables["events-t"])            # events go after the vault
    store.append(state, "agent.log", {"text": "still alive"}, message="still alive")
    await store.flush_events()
    assert sum(k[0] == rid for k in ddb.tables["events-t"]) == len(state.events)

    s3.deny.clear()
    out = await rundelete.delete_run(runtime(aws, store), state)
    assert out["vault_files"] == 1 and out["events"] == len(state.events)
    assert not any(k.startswith(f"runs/{rid}/") for k in s3.objects) and (rid,) not in ddb.tables["runs-t"]
    assert all(k in s3.objects for k in CACHE_FILES)


def test_s3_delete_refuses_keys_outside_the_run(short_settings):
    aws, store, s3, ddb, log = aws_store(short_settings)
    with pytest.raises(ValueError, match="not under runs/run_20261108_aaaaaa/"):
        store._s3_delete("run_20261108_aaaaaa", ["runs/run_20261108_aaaaaa/x", "cache/docs/abc.bin"])
    assert log == []


def test_bad_run_id_is_refused(short_settings):
    store = Store(short_settings)
    for bad in ("../cache", "run_x/../../cache", "cache"):
        with pytest.raises(ValueError):
            store.delete_run_data(bad)


def test_delete_holds_the_run_lock(short_settings, monkeypatch):
    store = Store(short_settings)
    state = finished_state(store)
    seen = []
    real = store._delete_local_run

    def spy(run_id):
        lock = lock_for(run_id)
        seen.append(lock._is_owned())                                 # RLock: held by this thread
        return real(run_id)

    monkeypatch.setattr(store, "_delete_local_run", spy)
    store.delete_run_data(state.run.run_id)
    assert seen == [True]
