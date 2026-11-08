"""AgentRunner: one registered agent that asks the model through the gateway with forced tool use and
validated Pydantic output, logs its transcript to the vault and rolls up its usage.

The gateway meters usage into the agent and the run. The runner reports it (agent.status) and keeps
the transcript (00-Meta/agents/<agent_id>.jsonl: full prompts and outputs, one JSON object per line).
The tool name is the output class name, or its TOOL_NAME ClassVar, so an agent module registers its
fake with `from .base import register_fake, tool_name` and register_fake(tool_name(MyOutput), fn)
(re-exported from bedrock.py through the gateway, so agent modules never import bedrock.py).
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ValidationError

from ..contract import m
from ..gateway import Blocked, InvokeResult, ToolSpec, register_fake, register_fake_scan
from ..ids import now_iso
from ..runtime import StopRequested

if TYPE_CHECKING:
    from ..runtime import RunContext

__all__ = ["AgentRunner", "AgentOutputError", "tool_name", "tool_spec", "coerce_json_strings", "register_fake",
           "register_fake_scan", "instructions_block"]

TERMINAL = ("done", "failed", "stopped")
_PREVIOUS_INPUT_CHARS = 4000
# Roles that never get a fork's run-wide instructions: judges are checks, and the analyst's instructions
# must never steer a check (CONTRACT.md 13: every check still applies to what the agents produce).
NO_RUN_INSTRUCTIONS = ("verifier",)


class AgentOutputError(ValueError):
    """The model's tool input failed validation on both tries."""


# ---------------------------------------------------------------------------
# Tool spec from a Pydantic output model
# ---------------------------------------------------------------------------


def tool_name(output: type[BaseModel]) -> str:
    """Bedrock tool names allow [A-Za-z0-9_-], 64 characters."""
    name = getattr(output, "TOOL_NAME", None) or output.__name__
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(name))[:64]


def inline_refs(schema: dict) -> dict:
    """Replace local $defs references with the definitions, so the model sees one flat schema.

    A recursive definition is left as a $ref, and $defs is kept for it.
    """
    defs = schema.get("$defs", {})

    def walk(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, list):
            return [walk(x, seen) for x in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.rsplit("/", 1)[-1]
            if name in defs and name not in seen:
                rest = {k: walk(v, seen) for k, v in node.items() if k != "$ref"}
                return {**walk(defs[name], seen | {name}), **rest}
            return node
        return {k: walk(v, seen) for k, v in node.items() if k != "$defs"}

    out = walk(schema, frozenset())
    if defs and '"$ref"' in json.dumps(out):
        out["$defs"] = defs
    return out


def tool_spec(output: type[BaseModel]) -> ToolSpec:
    """The one forced tool for an output model: its name, docstring and inlined JSON schema."""
    doc = (output.__doc__ or "").strip()
    description = doc or f"Return the result as {output.__name__}."
    return ToolSpec(name=tool_name(output), description=description[:1000],
                    input_schema=inline_refs(output.model_json_schema()))


# ---------------------------------------------------------------------------
# JSON strings where the schema wants an array or object
# ---------------------------------------------------------------------------
# Sonnet 5 sometimes sends a nested array or object field as a JSON-encoded string, and sometimes the
# whole input wrapped once more ({"sections": "{\"sections\": [...]}"}, seen live in R0). Code parses
# those back before validation. Only strings at a place where the schema allows an array or an object
# and no string are touched, and a parse is kept only when it gives that container type. Scalars are
# never coerced: "5" stays "5" and pydantic decides.

_MAX_REF_HOPS = 32


def _resolve(node: Any, defs: dict) -> dict:
    """Follow a local $ref (inline_refs leaves them only for recursive definitions)."""
    for _ in range(_MAX_REF_HOPS):
        if not isinstance(node, dict):
            return {}
        ref = node.get("$ref")
        if not (isinstance(ref, str) and ref.startswith("#/$defs/")):
            return node
        target = defs.get(ref.rsplit("/", 1)[-1])
        if not isinstance(target, dict):
            return {}
        node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    return {}


def _branches(node: Any, defs: dict) -> list[dict]:
    """The schemas a value may match: anyOf / oneOf alternatives (Optional, unions), else the node."""
    node = _resolve(node, defs)
    alts = node.get("anyOf") or node.get("oneOf")
    if isinstance(alts, list):
        return [b for a in alts for b in _branches(a, defs)]
    all_of = node.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1:
        return _branches({**_resolve(all_of[0], defs), **{k: v for k, v in node.items() if k != "allOf"}}, defs)
    return [node]


def _types(branch: dict) -> set[str]:
    t = branch.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    if "properties" in branch or "additionalProperties" in branch:
        return {"object"}
    if "items" in branch or "prefixItems" in branch:
        return {"array"}
    return set()                    # Any, const or enum without a type: never coerced


def _kinds(branches: list[dict]) -> set[str]:
    """Every type the alternatives allow, null aside."""
    return set().union(*(_types(b) for b in branches)) - {"null"}


def _is_kind(value: Any, kinds: set[str]) -> bool:
    return isinstance(value, list) and "array" in kinds or isinstance(value, dict) and "object" in kinds


def _object_branch(branches: list[dict], value: dict) -> dict | None:
    """The object alternative that fits the value best: a matching const first, then the most known keys."""
    objs = [b for b in branches if "object" in _types(b)]

    def score(b: dict) -> tuple[int, int]:
        props = b.get("properties") or {}
        consts = sum(1 for k, p in props.items() if isinstance(p, dict) and "const" in p
                     and value.get(k) == p["const"])
        return consts, sum(1 for k in value if k in props)

    return max(objs, key=score) if objs else None


def _from_json_string(text: str, kinds: set[str], name: str | None, parent_props: dict
                      ) -> tuple[Any, dict, bool]:
    """(value, wrapper siblings, coerced). A string that parses to the wanted container replaces itself.
    A parse that gives the parent object instead ({name: <container>, ...}) is unwrapped, and its other
    keys come back as siblings for the parent to fill where they are missing."""
    if "string" in kinds or not kinds & {"array", "object"}:
        return text, {}, False
    try:
        parsed = json.loads(text)
        if isinstance(parsed, str):                     # encoded twice
            parsed = json.loads(parsed)
    except ValueError:
        return text, {}, False
    if _is_kind(parsed, kinds):
        return parsed, {}, True
    if (isinstance(parsed, dict) and name is not None and name in parsed and _is_kind(parsed[name], kinds)
            and all(k in parent_props for k in parsed)):
        return parsed[name], {k: v for k, v in parsed.items() if k != name}, True
    return text, {}, False


def _coerce(value: Any, schema: Any, defs: dict, path: str, hits: list[str]) -> Any:
    """A copy of value with every JSON string the schema wants as a container parsed, recursively."""
    branches = _branches(schema, defs)
    if isinstance(value, str):
        value, _, ok = _from_json_string(value, _kinds(branches), None, {})
        if not ok:
            return value
        hits.append(path or "(root)")
    if isinstance(value, dict):
        branch = _object_branch(branches, value)
        return value if branch is None else _coerce_object(value, branch, defs, path, hits)
    if isinstance(value, list):
        arrays = [b for b in branches if "array" in _types(b)]
        if not arrays:
            return value
        items, prefix = arrays[0].get("items"), arrays[0].get("prefixItems") or []
        out = []
        for i, v in enumerate(value):
            sub = prefix[i] if i < len(prefix) else items
            out.append(_coerce(v, sub, defs, f"{path}[{i}]", hits) if isinstance(sub, dict) else v)
        return out
    return value


def _coerce_object(value: dict, branch: dict, defs: dict, path: str, hits: list[str]) -> dict:
    props = branch.get("properties") or {}
    extra = branch.get("additionalProperties")
    out: dict = {}
    fill: dict = {}
    for k, v in value.items():
        sub = props.get(k, extra)
        if not isinstance(sub, dict):
            out[k] = v
            continue
        where = f"{path}.{k}" if path else str(k)
        if isinstance(v, str):
            v, siblings, ok = _from_json_string(v, _kinds(_branches(sub, defs)), str(k), props)
            if not ok:
                out[k] = v
                continue
            hits.append(where)
            fill.update({sk: sv for sk, sv in siblings.items() if sk not in value})
        out[k] = _coerce(v, sub, defs, where, hits)
    for k, v in fill.items():
        where = f"{path}.{k}" if path else str(k)
        hits.append(where)
        out[k] = _coerce(v, props[k], defs, where, hits)
    return out


def coerce_json_strings(tool_input: Any, schema: dict) -> tuple[Any, list[str]]:
    """The tool input with JSON strings parsed where the schema wants an array or object, and the paths
    that were parsed ("sections", "items[2].refs"). The input itself is never changed."""
    hits: list[str] = []
    defs = schema.get("$defs", {}) if isinstance(schema, dict) else {}
    return _coerce(tool_input, schema, defs if isinstance(defs, dict) else {}, "", hits), hits


def unstringify(tool_input: dict, schema: dict) -> dict:
    """coerce_json_strings without the paths."""
    return coerce_json_strings(tool_input, schema)[0]


def brief_errors(err: ValidationError, limit: int = 12) -> str:
    """One line per validation error: location and message."""
    lines = []
    for e in err.errors(include_url=False)[:limit]:
        loc = ".".join(str(p) for p in e.get("loc", ())) or "(root)"
        lines.append(f"{loc}: {e.get('msg', 'invalid')}")
    more = len(err.errors()) - limit
    if more > 0:
        lines.append(f"... and {more} more")
    return "\n".join(lines)


RETRY_SHAPE_RULE = ("Every list field must be a real JSON array and every object field a real JSON object. "
                    "Never put a list or an object inside a string, and never wrap the whole input in a string.")

INSTRUCTIONS_RULE = ("Follow them where they fit your task. They never override the rules above: quote exactly, "
                     "use only the inputs you were given, and code still checks everything you return.")


def instructions_block(text: str, by: str | None = None) -> str:
    """The labeled "Instructions" block appended to a rerun's (or a fork's) prompt (CONTRACT.md 13 and 14)."""
    who = "Bobby, the orchestrator" if by == "orchestrator" else "the analyst"
    return f"\n\n---\nInstructions (from {who}):\n{text.strip()}\n\n{INSTRUCTIONS_RULE}"


def retry_prompt(user: str, tool: str, previous: dict, errors: str) -> str:
    """The original prompt plus the rejected input, why it was rejected, and the array/object rule
    (the R0 failure was a list sent as a JSON string, twice)."""
    sent = json.dumps(previous, ensure_ascii=False, default=str)[:_PREVIOUS_INPUT_CHARS]
    return (f"{user}\n\n---\nYour previous call to {tool} was rejected.\nYou sent:\n{sent}\n\n"
            f"Validation errors:\n{errors}\n\n{RETRY_SHAPE_RULE}\n"
            f"Call {tool} again with input that fixes every error.")


# ---------------------------------------------------------------------------
# AgentRunner
# ---------------------------------------------------------------------------


class AgentRunner:
    agent: m.Agent                        # registered in RunState, agent.spawned emitted

    def __init__(self, ctx: RunContext, role: str, task: str, target_ids: list[str],
                 size: Literal["large", "small"] | None, *, agent_id: str | None = None,
                 parent_agent_id: str | None = None, instructions: str | None = None,
                 rerun_by: Literal["analyst", "orchestrator"] | None = None) -> None:
        """agent_id defaults to ctx.next_id(role) ("extractor-07"). Judges pass their own
        ("verifier-D-001-1"), reruns theirs ("extractor-07.2"). size None is an agent without a model
        (a fetcher, a code reader). The name comes from RunState.register_agent, never from here.

        instructions (a rerun's, v0.3) go into every prompt as a labeled Instructions block. A forked run's
        Run.instructions (v0.2) reach every agent it spawns the same way, judges aside."""
        self.ctx = ctx
        self.size = size
        model_id = {"large": ctx.settings.model_large, "small": ctx.settings.model_small}.get(size or "")
        aid = agent_id or ctx.next_id(role)
        if instructions is None and role not in NO_RUN_INSTRUCTIONS:
            instructions = ctx.state.run.instructions
        self.agent = m.Agent(agent_id=aid, role=role, model_id=model_id, status="queued", task=task,
                             target_ids=list(target_ids), parent_agent_id=parent_agent_id,
                             instructions=instructions, rerun_by=rerun_by)
        ctx.state.register_agent(self.agent)
        self._lines: list[str] = []
        self._transcript_failed = False
        self._preload = False
        self._track = True
        self._line("spawned", role=role, task=task, target_ids=list(target_ids), model_id=model_id,
                   parent_agent_id=parent_agent_id, instructions=instructions, rerun_by=rerun_by)
        ctx.emit("agent.spawned", m.AgentSpawnedData(agent=self.agent.model_copy(deep=True)),
                 message=f"{aid} spawned: {task}", agent_id=aid)

    @classmethod
    def attach(cls, ctx: RunContext, agent_id: str, size: Literal["large", "small"] | None = None, *,
               track_status: bool = True) -> AgentRunner:
        """A runner for an agent that is already registered: no new agent and no agent.spawned. For Bobby
        at each gate and for chat with any agent, so the calls run as that agent (its stop rule, its usage).
        size defaults to the agent's own model. The transcript keeps the lines already saved. With
        track_status=False, ask() never changes the agent's status (a chat reply from a finished agent).
        Raises KeyError for an unknown agent."""
        agent = ctx.state.agents[agent_id]
        self = cls.__new__(cls)
        self.ctx = ctx
        if size is None:
            size = {ctx.settings.model_large: "large", ctx.settings.model_small: "small"}.get(agent.model_id or "")
        self.size = size
        self.agent = agent
        self._lines = []
        self._transcript_failed = False
        self._preload = True
        self._track = track_status
        return self

    def start(self) -> None:
        """Mark the agent working now (Bobby works from spawn to the end of the run)."""
        self._set_status("working")

    # -- events and transcript -------------------------------------------------

    @property
    def agent_id(self) -> str:
        return self.agent.agent_id

    def _line(self, kind: str, **fields: Any) -> None:
        self._lines.append(json.dumps({"ts": now_iso(), "agent_id": self.agent_id, "kind": kind, **fields},
                                      ensure_ascii=False, default=str))

    async def _save(self) -> None:
        """Rewrite the transcript file. A vault failure is logged, never fatal to the agent."""
        relpath = f"00-Meta/agents/{self.agent_id}.jsonl"
        if self._preload:                    # an attached runner keeps what earlier runners saved
            self._preload = False
            try:
                old = await asyncio.to_thread(self.ctx.vault.get_text, self.ctx.state.run.run_id, relpath)
            except Exception:
                old = ""
            self._lines = [ln for ln in old.splitlines() if ln.strip()] + self._lines
        text = "\n".join(self._lines) + "\n"
        try:
            path = await asyncio.to_thread(self.ctx.vault.put_text, self.ctx.state.run.run_id, relpath, text)
        except Exception as e:
            if not self._transcript_failed:
                self._transcript_failed = True
                self._log(f"Transcript not saved: {type(e).__name__}: {e}", level="warn")
            return
        if isinstance(path, str) and path:
            self.agent.transcript_path = path

    def _log(self, text: str, level: str = "info") -> None:
        self.ctx.emit("agent.log", m.AgentLogData(text=text, transcript_path=self.agent.transcript_path),
                      message=f"{self.agent_id}: {text}", agent_id=self.agent_id, level=level)

    def _emit_status(self) -> None:
        if not self._track:
            return
        a = self.agent
        self.ctx.emit("agent.status", m.AgentStatusData(agent_id=a.agent_id, status=a.status, task=a.task,
                                                        usage=a.usage.model_copy()),
                      message=f"{a.agent_id} {a.status}", agent_id=a.agent_id)

    def _set_status(self, status: str) -> None:
        a = self.agent
        if a.status == status or not self._track:
            return
        a.status = status
        if status == "working" and a.started_at is None:
            a.started_at = now_iso()
        if status in TERMINAL:
            a.finished_at = now_iso()
        self._emit_status()

    # -- the model call ----------------------------------------------------------

    async def _invoke(self, system: str, prompt: str, spec: ToolSpec, max_tokens: int) -> InvokeResult:
        try:
            return await self.ctx.gateway.invoke(self.agent, self.size, system=system, user=prompt, tool=spec,
                                                 max_tokens=max_tokens)
        except Blocked as e:
            self._line("blocked", request_id=e.request_id, rule_id=e.rule_id, reason=e.reason)
            await self._save()
            self._log(f"{spec.name} blocked by the gateway: {e.reason}", level="warn")
            if getattr(self.ctx.gateway, "kill_switch_on", False):
                self.finish("stopped")
                raise StopRequested(e.reason) from e
            raise

    def _coerced(self, tool_input: dict, spec: ToolSpec, attempt: int) -> Any:
        """The tool input with JSON-string arrays and objects parsed, noted in the transcript and the log."""
        fixed, paths = coerce_json_strings(tool_input, spec.input_schema)
        if paths:
            self._line("coerced", attempt=attempt, paths=paths)
            shown = ", ".join(paths[:6]) + (f" and {len(paths) - 6} more" if len(paths) > 6 else "")
            self._log(f"{spec.name}: parsed {len(paths)} field(s) the model sent as JSON strings ({shown})")
        return fixed

    async def ask(self, system: str, user: str, output: type[BaseModel], *, max_tokens: int = 4096) -> BaseModel:
        """Forced tool use through ctx.gateway.invoke, validated with output.model_validate (after
        coerce_json_strings parses arrays and objects the model sent as JSON strings), one retry that
        shows the model its validation error. Transcript to 00-Meta/agents/<agent_id>.jsonl.

        Raises Blocked (gateway deny), StopRequested (kill switch, agent marked stopped) or
        AgentOutputError (invalid twice). The caller still calls finish() for the other outcomes.
        """
        spec = tool_spec(output)
        self._set_status("working")
        if self.agent.instructions:
            user = user + instructions_block(self.agent.instructions, self.agent.rerun_by)
        prompt, errors = user, ""
        for attempt in (1, 2):
            self._line("request", attempt=attempt, tool=spec.name, size=self.size, max_tokens=max_tokens,
                       system=system, user=prompt)
            res = await self._invoke(system, prompt, spec, max_tokens)
            self._line("response", attempt=attempt, request_id=res.request_id, model_id=res.model_id,
                       stop_reason=res.stop_reason, tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                       cost_usd=res.cost_usd, tool_input=res.tool_input)
            try:
                if res.stop_reason == "max_tokens":
                    raise AgentOutputError(f"the output was cut off at {max_tokens} tokens")
                obj = output.model_validate(self._coerced(res.tool_input, spec, attempt))
            except (ValidationError, AgentOutputError) as e:
                errors = brief_errors(e) if isinstance(e, ValidationError) else f"(root): {e}. Answer more briefly."
                self._line("invalid", attempt=attempt, errors=errors)
                await self._save()
                first = errors.splitlines()[0] if errors else "invalid"
                self._log(f"{spec.name} output invalid on try {attempt} ({first})", level="warn")
                prompt = retry_prompt(user, spec.name, res.tool_input, errors)
                continue
            await self._save()
            self._log(f"{spec.name} answered on try {attempt} ({res.tokens_in} in / {res.tokens_out} out, "
                      f"${res.cost_usd:.4f})")
            self._emit_status()
            return obj
        self._emit_status()
        raise AgentOutputError(f"{self.agent_id}: {spec.name} output failed validation twice:\n{errors}")

    def finish(self, status: str = "done") -> None:
        """Mark the agent done, failed or stopped. The first terminal status wins."""
        if status not in TERMINAL:
            raise ValueError(f"finish status must be one of {TERMINAL}, not {status!r}")
        if self.agent.status in TERMINAL:
            return
        self._set_status(status)

    # -- with AgentRunner(...) as r: finishes as done, failed or stopped ----------

    def __enter__(self) -> AgentRunner:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.finish("done")
        elif issubclass(exc_type, StopRequested):
            self.finish("stopped")
        else:
            self.finish("failed")
        return False
