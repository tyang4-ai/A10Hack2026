"""The gateway: the only path to the network and to Bedrock. It enforces the policy, the kill switch
and the budget, meters every call, emits one gateway.request per decision, and fails closed."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .contract import m

if TYPE_CHECKING:
    from .runtime import RunContext


class Blocked(Exception):
    """A deny. rule_id is None when the default deny decided it."""

    def __init__(self, request_id: str, rule_id: str | None, reason: str) -> None:
        super().__init__(f"{request_id} blocked ({rule_id or 'default deny'}): {reason}")
        self.request_id = request_id
        self.rule_id = rule_id
        self.reason = reason


@dataclass
class FetchResult:
    url: str
    final_url: str
    http_status: int
    content_type: str
    body: bytes
    request_id: str


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict          # JSON Schema of the one forced tool


@dataclass
class InvokeResult:
    tool_input: dict
    tokens_in: int
    tokens_out: int
    cost_usd: float
    request_id: str


class Gateway:
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def build_policy(self) -> m.Policy:
        """Policy from the sources' approvals: R-sys-models, R-allow-<S>, R-reject-<S>, default deny."""
        raise NotImplementedError("B-gateway")

    def revoke(self, source_id: str, reason: str | None) -> m.Policy:
        raise NotImplementedError("B-gateway")

    def set_kill_switch(self, reason: str | None) -> None:
        raise NotImplementedError("B-gateway")

    def decide(self, agent: m.Agent, action: str, target: str) -> tuple[str, str | None, str]:
        """(decision, rule_id, reason) in the CONTRACT.md section 6 order. Any exception inside means blocked."""
        raise NotImplementedError("B-gateway")

    async def fetch(self, agent: m.Agent, url: str, *, source_id: str | None = None,
                    why: str = "approved source") -> FetchResult:
        """Raises Blocked."""
        raise NotImplementedError("B-gateway")

    async def invoke(self, agent: m.Agent, size: Literal["large", "small"], *, system: str, user: str,
                     tool: ToolSpec, max_tokens: int = 4096) -> InvokeResult:
        """Raises Blocked."""
        raise NotImplementedError("B-gateway")

    async def scan(self, agent: m.Agent, url: str, source_id: str, text: str) -> m.ScanResult:
        """Guardrails tripwire. When it intervenes it also emits gateway.request with decision "flagged"."""
        raise NotImplementedError("B-gateway")
