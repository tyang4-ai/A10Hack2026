"""AgentRunner: one registered agent that asks the model through the gateway with forced tool use and
validated Pydantic output, logs its transcript to the vault and rolls up its usage."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from ..contract import m

if TYPE_CHECKING:
    from ..runtime import RunContext


class AgentRunner:
    agent: m.Agent                        # registered in RunState, agent.spawned emitted

    def __init__(self, ctx: RunContext, role: str, task: str, target_ids: list[str],
                 size: Literal["large", "small"]) -> None:
        raise NotImplementedError("B-gateway")

    async def ask(self, system: str, user: str, output: type[BaseModel], *, max_tokens: int = 4096) -> BaseModel:
        """Forced tool use through ctx.gateway.invoke, validated with output.model_validate, one retry
        that shows the model its validation error. Transcript to 00-Meta/agents/<agent_id>.jsonl."""
        raise NotImplementedError("B-gateway")

    def finish(self, status: str = "done") -> None:
        raise NotImplementedError("B-gateway")
