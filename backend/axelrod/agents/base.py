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

__all__ = ["AgentRunner", "AgentOutputError", "tool_name", "tool_spec", "register_fake", "register_fake_scan"]

TERMINAL = ("done", "failed", "stopped")
_PREVIOUS_INPUT_CHARS = 4000


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


def unstringify(tool_input: dict, schema: dict) -> dict:
    """Models sometimes send a nested array or object as a JSON string. Parse those back."""
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    out = dict(tool_input)
    for key, value in out.items():
        want = (props.get(key) or {}).get("type")
        if isinstance(value, str) and want in ("array", "object"):
            try:
                parsed = json.loads(value)
            except ValueError:
                continue
            if isinstance(parsed, list if want == "array" else dict):
                out[key] = parsed
    return out


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


def retry_prompt(user: str, tool: str, previous: dict, errors: str) -> str:
    """The original prompt plus the rejected input and why it was rejected."""
    sent = json.dumps(previous, ensure_ascii=False, default=str)[:_PREVIOUS_INPUT_CHARS]
    return (f"{user}\n\n---\nYour previous call to {tool} was rejected.\nYou sent:\n{sent}\n\n"
            f"Validation errors:\n{errors}\n\nCall {tool} again with input that fixes every error.")


# ---------------------------------------------------------------------------
# AgentRunner
# ---------------------------------------------------------------------------


class AgentRunner:
    agent: m.Agent                        # registered in RunState, agent.spawned emitted

    def __init__(self, ctx: RunContext, role: str, task: str, target_ids: list[str],
                 size: Literal["large", "small"] | None, *, agent_id: str | None = None) -> None:
        """agent_id defaults to ctx.next_id(role) ("extractor-07"). Judges pass their own
        ("verifier-D-001-1"). size None is an agent without a model (a fetcher, a code reader)."""
        self.ctx = ctx
        self.size = size
        model_id = {"large": ctx.settings.model_large, "small": ctx.settings.model_small}.get(size or "")
        aid = agent_id or ctx.next_id(role)
        self.agent = m.Agent(agent_id=aid, role=role, model_id=model_id, status="queued", task=task,
                             target_ids=list(target_ids))
        ctx.state.agents[aid] = self.agent
        self._lines: list[str] = []
        self._transcript_failed = False
        self._line("spawned", role=role, task=task, target_ids=list(target_ids), model_id=model_id)
        ctx.emit("agent.spawned", m.AgentSpawnedData(agent=self.agent.model_copy(deep=True)),
                 message=f"{aid} spawned: {task}", agent_id=aid)

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
        a = self.agent
        self.ctx.emit("agent.status", m.AgentStatusData(agent_id=a.agent_id, status=a.status, task=a.task,
                                                        usage=a.usage.model_copy()),
                      message=f"{a.agent_id} {a.status}", agent_id=a.agent_id)

    def _set_status(self, status: str) -> None:
        a = self.agent
        if a.status == status:
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

    async def ask(self, system: str, user: str, output: type[BaseModel], *, max_tokens: int = 4096) -> BaseModel:
        """Forced tool use through ctx.gateway.invoke, validated with output.model_validate, one retry
        that shows the model its validation error. Transcript to 00-Meta/agents/<agent_id>.jsonl.

        Raises Blocked (gateway deny), StopRequested (kill switch, agent marked stopped) or
        AgentOutputError (invalid twice). The caller still calls finish() for the other outcomes.
        """
        spec = tool_spec(output)
        self._set_status("working")
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
                obj = output.model_validate(unstringify(res.tool_input, spec.input_schema))
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
