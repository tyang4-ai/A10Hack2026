"""B-fix-brief: AgentRunner parses arrays and objects that the model sent as JSON strings.

The R0 rehearsal failed at summarize because Sonnet 5 sent brief_draft as
{"sections": "{\"sections\": [...]}"} on both tries. The offline tests below use fakes (simulated model
answers) that send the same shapes. The live test (AXELROD_LIVE=1) makes one real summarizer call on the
mock fixture report (fictional company).
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, ValidationError

from backend.axelrod import bedrock
from backend.axelrod.agents import base, summarizer
from backend.axelrod.agents.base import AgentOutputError, AgentRunner, coerce_json_strings, tool_name, tool_spec
from backend.axelrod.contract import m
from backend.axelrod.runtime import RunContext
from backend.axelrod.stages import summarize
from backend.axelrod.store import Store

BRIEF = tool_name(summarizer.BriefDraft)


# ---------------------------------------------------------------------------
# Output models for the tests
# ---------------------------------------------------------------------------


class Row(BaseModel):
    name: str
    n: int
    refs: list[str] = Field(default_factory=list)


class Inner(BaseModel):
    note: str
    rows: list[Row] = Field(default_factory=list)


class Meta(BaseModel):
    tags: list[str]
    inner: Inner | None = None


class Holder(BaseModel):
    """Rows, a nested object and two scalar fields."""
    rows: list[Row] = Field(default_factory=list)
    meta: Meta | None = None
    label: str = ""
    count: int = 0
    either: str | list[str] = ""


class Node(BaseModel):
    name: str
    children: list["Node"] = Field(default_factory=list)


SCHEMA = tool_spec(Holder).input_schema
ROWS = [{"name": "a", "n": 1, "refs": ["D-001"]}, {"name": "b", "n": 2}]


def coerce(tool_input: dict, schema: dict = SCHEMA) -> tuple[dict, list[str]]:
    return coerce_json_strings(tool_input, schema)


# ---------------------------------------------------------------------------
# coerce_json_strings
# ---------------------------------------------------------------------------


def test_stringified_list_is_parsed():
    fixed, paths = coerce({"rows": json.dumps(ROWS)})
    assert fixed["rows"] == ROWS and paths == ["rows"]
    assert Holder.model_validate(fixed).rows[1].name == "b"


def test_stringified_nested_object_is_parsed_recursively():
    meta = {"tags": json.dumps(["x", "y"]), "inner": json.dumps({"note": "n", "rows": json.dumps(ROWS)})}
    fixed, paths = coerce({"meta": json.dumps(meta)})
    assert paths == ["meta", "meta.tags", "meta.inner", "meta.inner.rows"]
    out = Holder.model_validate(fixed)
    assert out.meta.tags == ["x", "y"] and out.meta.inner.rows[0].refs == ["D-001"]


def test_strings_inside_list_items_are_parsed():
    rows = [{"name": "a", "n": 1, "refs": '["D-001", "D-002"]'}, json.dumps({"name": "b", "n": 2})]
    fixed, paths = coerce({"rows": rows})
    assert paths == ["rows[0].refs", "rows[1]"]
    assert Holder.model_validate(fixed).rows[0].refs == ["D-001", "D-002"]


def test_a_string_that_is_not_json_is_left_and_still_fails_validation():
    bad = {"rows": "[{name: a, n: 1}"}
    fixed, paths = coerce(bad)
    assert fixed == bad and paths == []
    with pytest.raises(ValidationError):
        Holder.model_validate(fixed)


def test_json_of_the_wrong_type_is_left_alone():
    bad = {"rows": '{"name": "a", "n": 1}', "meta": '["x"]'}
    fixed, paths = coerce(bad)
    assert fixed == bad and paths == []
    with pytest.raises(ValidationError):
        Holder.model_validate(fixed)


def test_scalar_string_fields_are_never_coerced():
    raw = {"label": '["a", "b"]', "count": "5", "either": '["a"]'}
    fixed, paths = coerce(raw)
    assert fixed == raw and paths == []                 # str stays str, "5" is pydantic's business
    out = Holder.model_validate(fixed)
    assert out.label == '["a", "b"]' and out.either == '["a"]'


def test_the_r0_wrapper_is_unwrapped_and_siblings_fill_what_is_missing():
    """Live R0 shape: the whole input as a JSON string inside its only list field."""
    fixed, paths = coerce({"rows": json.dumps({"rows": ROWS, "label": "from the wrapper"}), "count": 3})
    assert fixed["rows"] == ROWS and fixed["label"] == "from the wrapper" and fixed["count"] == 3
    assert paths == ["rows", "label"]
    # A wrapper with keys the parent does not have is not the parent: left alone.
    odd = {"rows": json.dumps({"rows": ROWS, "unknown": 1})}
    assert coerce(odd) == (odd, [])


def test_double_encoded_and_recursive_refs():
    fixed, paths = coerce({"rows": json.dumps(json.dumps(ROWS))})
    assert fixed["rows"] == ROWS and paths == ["rows"]
    node_schema = tool_spec(Node).input_schema
    assert "$defs" in node_schema                       # recursive: the $ref is kept
    tree = {"name": "root", "children": json.dumps([{"name": "a", "children": '[{"name": "b"}]'}])}
    fixed, paths = coerce(tree, node_schema)
    assert paths == ["children", "children[0].children"]
    assert Node.model_validate(fixed).children[0].children[0].name == "b"


def test_the_input_is_not_changed():
    raw = {"rows": json.dumps(ROWS), "meta": {"tags": '["x"]'}}
    before = copy.deepcopy(raw)
    coerce(raw)
    assert raw == before


def test_the_r0_brief_shape_validates_after_coercion(fixture_report):
    """brief_draft as Sonnet 5 sent it in R0, built here from the offline fake brief (simulated)."""
    sections = [{"heading": "Bottom line", "blocks": [{"type": "paragraph", "sentences": [
        {"text": "A sentence.", "mode": "derived", "derived_from": ["rollup"]}]}]},
                {"heading": "Charts", "blocks": [{"type": "chart", "chart_id": "G-001",
                                                  "caption": json.dumps({"text": "A chart.", "mode": "copied",
                                                                         "derived_from": ["G-001"]})}]}]
    raw = {"sections": json.dumps({"sections": sections}, indent=1)}
    fixed, paths = coerce_json_strings(raw, tool_spec(summarizer.BriefDraft).input_schema)
    assert paths == ["sections", "sections[1].blocks[0].caption"]
    draft = summarizer.BriefDraft.model_validate(fixed)
    assert draft.sections[1].blocks[0].caption.derived_from == ["G-001"]


# ---------------------------------------------------------------------------
# AgentRunner with fakes (FAKE_MODELS=1)
# ---------------------------------------------------------------------------


def make_ctx(settings, company: str = "Coerce Test Co") -> RunContext:
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company=company))
    return RunContext.create(state, store, store.vault, settings)


def events(ctx: RunContext, type_: str) -> list:
    return [e for e in ctx.state.events if e.type == type_]


@pytest.fixture
def holder_fake():
    """Register a fake for Holder's tool for one test, then remove it."""
    name = tool_name(Holder)

    def use(fn):
        base.register_fake(name, fn)

    yield use
    bedrock.unregister_fake(name)


async def test_runner_parses_and_logs_without_a_retry(settings, holder_fake):
    calls = []

    def fake(system, user, schema):
        calls.append(user)
        return {"rows": json.dumps({"rows": ROWS}), "meta": {"tags": '["x"]'}, "label": '["kept"]'}

    holder_fake(fake)
    ctx = make_ctx(settings)
    with AgentRunner(ctx, "extractor", "coerce test", [], "small") as r:
        out = await r.ask("s", "u", Holder)
    assert len(calls) == 1 and out.rows[0].refs == ["D-001"] and out.meta.tags == ["x"]
    assert out.label == '["kept"]'
    logs = [e.data.text for e in events(ctx, "agent.log")]
    assert any("parsed 2 field(s) the model sent as JSON strings (rows, meta.tags)" in t for t in logs)
    lines = [json.loads(x) for x in ctx.vault.get_text(ctx.run_id, f"00-Meta/agents/{r.agent_id}.jsonl").splitlines()]
    assert [x["kind"] for x in lines] == ["spawned", "request", "response", "coerced"]
    assert lines[3]["paths"] == ["rows", "meta.tags"]
    assert isinstance(lines[2]["tool_input"]["rows"], str)      # the transcript keeps what the model sent


async def test_runner_retry_says_lists_must_be_real_arrays(settings, holder_fake):
    prompts = []

    def fake(system, user, schema):
        prompts.append(user)
        return {"rows": "rows: a, b"}                              # not JSON: invalid on both tries

    holder_fake(fake)
    ctx = make_ctx(settings)
    with pytest.raises(AgentOutputError):
        with AgentRunner(ctx, "extractor", "coerce test", [], "small") as r:
            await r.ask("s", "the task", Holder)
    assert len(prompts) == 2 and prompts[1].startswith("the task")
    assert base.RETRY_SHAPE_RULE in prompts[1] and "real JSON array" in prompts[1]
    assert r.agent.status == "failed"


def report_ctx(settings, bundle: dict, report: m.AgentReport) -> RunContext:
    """A run holding the mock fixture's final report (fictional company), ready for summarize."""
    store = Store(settings)
    state = store.create_run(m.CreateRunRequest(company=report.company, ticker=report.ticker))
    state.sources = [m.Source.model_validate(s) for s in copy.deepcopy(bundle["sources"])]
    state.items = {i.id: i for i in report.items}
    state.holes = list(report.holes)
    state.rollup = report.rollup
    state.report_certainty = report.certainty
    state.report_ready = True
    state.as_of = "2026-11-08"
    state.run.stage = "summarize"
    return RunContext.create(state, store, store.vault, settings)


async def test_summarize_survives_the_r0_failure(settings, fixture_bundle, fixture_report):
    """The summarizer fake answers exactly as Sonnet 5 did in R0 (simulated): one call, no retry."""
    calls = []

    def r0_shape(system, user, schema):
        calls.append(user)
        return {"sections": json.dumps(summarizer.fake_brief(system, user, schema), indent=1)}

    bedrock.register_fake(BRIEF, r0_shape)
    try:
        ctx = report_ctx(settings, fixture_bundle, fixture_report)
        await summarize.run(ctx)
    finally:
        bedrock.register_fake(BRIEF, summarizer.fake_brief)
    assert len(calls) == 1
    assert ctx.state.summary is not None and ctx.state.summary.stats.sentences > 0
    agent = ctx.state.agents["summarizer-01"]
    assert agent.status == "done" and agent.usage.model_calls == 1
    assert any("brief_draft: parsed 1 field(s) the model sent as JSON strings (sections)" in e.data.text
               for e in events(ctx, "agent.log"))
    assert "JSON array" in calls[0] and "not a string" in calls[0]


def test_brief_tool_description_asks_for_a_real_array():
    spec = tool_spec(summarizer.BriefDraft)
    assert "never a string" in spec.description
    assert "not a string" in spec.input_schema["properties"]["sections"]["description"]


# ---------------------------------------------------------------------------
# Live (AXELROD_LIVE=1): one real summarizer call, local store and vault
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_summarizer_on_the_fixture_report_validates(settings, fixture_bundle, fixture_report):
    """One Sonnet call through the gateway on the mock fixture report (fictional company). Under $0.30."""
    data_dir = Path(os.environ.get("DATA_DIR") or "backend/.data/fixbrief")
    live = settings.model_copy(update={"store": "local", "fake_models": False, "data_dir": data_dir})
    ctx = report_ctx(live, fixture_bundle, fixture_report)
    await summarize.run(ctx)
    agent = ctx.state.agents["summarizer-01"]
    logs = [e.data.text for e in events(ctx, "agent.log") if e.agent_id == "summarizer-01"]
    print(f"\nrun {ctx.run_id} in {data_dir}\ncalls {agent.usage.model_calls}, "
          f"cost ${ctx.state.run.usage.cost_usd:.4f}\n" + "\n".join(logs))
    brief = ctx.state.summary
    assert agent.status == "done" and brief is not None and brief.stats.sentences > 0
    print(f"{brief.stats}")
    m.HumanReport.model_validate(brief.model_dump(mode="json"))
    assert ctx.state.run.usage.cost_usd < 0.30
