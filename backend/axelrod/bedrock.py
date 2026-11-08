"""Bedrock Converse with one forced tool, ApplyGuardrail, and the fake registry used when FAKE_MODELS=1.

Only gateway.py imports this module. boto3 clients are created on first use, never at import.
converse and apply_guardrail are suggested signatures: B-gateway may change them.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .gateway import ToolSpec

FakeFn = Callable[[str, str, dict], dict]       # fn(system, user, schema) -> tool input


def register_fake(tool_name: str, fn: FakeFn) -> None:
    """Register the fake answer for one tool name, used when FAKE_MODELS=1."""
    raise NotImplementedError("B-gateway")


async def converse(settings: Settings, model_id: str, *, system: str, user: str, tool: ToolSpec,
                   max_tokens: int = 4096) -> tuple[dict, int, int]:
    """One Converse call forcing `tool`. Returns (tool_input, tokens_in, tokens_out)."""
    raise NotImplementedError("B-gateway")


async def apply_guardrail(settings: Settings, text: str) -> dict:
    """ApplyGuardrail (source INPUT) on text, chunked to the API limit. Returns the raw verdict."""
    raise NotImplementedError("B-gateway")
