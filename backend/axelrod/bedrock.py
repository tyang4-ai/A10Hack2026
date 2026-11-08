"""Bedrock Converse with one forced tool, ApplyGuardrail, and the fake registry used when FAKE_MODELS=1.

Only gateway.py imports this module. The boto3 client is created on first use (in one of the gateway's worker
threads), never at import, so offline tests never touch AWS. With FAKE_MODELS=1 nothing here calls AWS:
Converse answers from the fakes registered per tool name, and the guardrail scan uses the fake scanner
if one is registered (simulated, and labelled so in its detail) or is skipped.
"""
from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings
    from .gateway import ToolSpec

FakeFn = Callable[[str, str, dict], dict]       # fn(system, user, schema) -> tool input
FakeScanFn = Callable[[str], bool]              # fn(text) -> True when it is a prompt attack

# ApplyGuardrail bills and throttles in text units of 1,000 characters. 10 units per call stays well
# under the on-demand per-second quota, and the overlap keeps an attack split across a cut visible.
GUARDRAIL_CHUNK_CHARS = 10_000
GUARDRAIL_OVERLAP_CHARS = 300

_FAKES: dict[str, FakeFn] = {}
_fake_scan: FakeScanFn | None = None
_clients: dict[tuple[str, int], Any] = {}
_executors: dict[int, ThreadPoolExecutor] = {}
_client_lock = threading.Lock()


@dataclass
class Reply:
    """One Converse answer. tool_input is {} when the model didn't call the tool."""

    tool_input: dict
    tokens_in: int
    tokens_out: int
    stop_reason: str


# ---------------------------------------------------------------------------
# Fakes (FAKE_MODELS=1)
# ---------------------------------------------------------------------------


def register_fake(tool_name: str, fn: FakeFn) -> None:
    """Register the fake answer for one tool name, used when FAKE_MODELS=1."""
    _FAKES[tool_name] = fn


def unregister_fake(tool_name: str) -> None:
    _FAKES.pop(tool_name, None)


def register_fake_scan(fn: FakeScanFn | None) -> None:
    """Fake guardrail for FAKE_MODELS=1 runs (simulated). None removes it, so scans are skipped."""
    global _fake_scan
    _fake_scan = fn


async def _fake_converse(system: str, user: str, tool: ToolSpec) -> Reply:
    fn = _FAKES.get(tool.name)
    if fn is None:
        raise LookupError(f"FAKE_MODELS=1 and no fake is registered for tool {tool.name!r}")
    out = fn(system, user, tool.input_schema)
    if inspect.isawaitable(out):
        out = await out
    if not isinstance(out, dict):
        raise TypeError(f"fake for {tool.name!r} returned {type(out).__name__}, not dict")
    # Fake usage: characters divided by 4, the schema counted as input like the real tool config.
    tokens_in = max(1, (len(system) + len(user) + len(json.dumps(tool.input_schema))) // 4)
    tokens_out = max(1, len(json.dumps(out, default=str)) // 4)
    return Reply(copy.deepcopy(out), tokens_in, tokens_out, "tool_use")


# ---------------------------------------------------------------------------
# Real Bedrock
# ---------------------------------------------------------------------------


def _pool_size(settings: Settings) -> int:
    """Room for every call both semaphores allow at once, plus a few guardrail scans."""
    return max(10, settings.max_concurrency_small + settings.max_concurrency_large + 4)


def _client(settings: Settings) -> Any:
    """One bedrock-runtime client per region. Adaptive retries, and a pool big enough for both semaphores."""
    pool = _pool_size(settings)
    key = (settings.aws_region, pool)
    with _client_lock:
        if key not in _clients:
            import boto3
            from botocore.config import Config

            _clients[key] = boto3.client(
                "bedrock-runtime",
                region_name=settings.aws_region,
                config=Config(retries={"mode": "adaptive", "max_attempts": 8}, max_pool_connections=pool,
                              connect_timeout=10, read_timeout=300),
            )
        return _clients[key]


def _executor(settings: Settings) -> ThreadPoolExecutor:
    """Our own threads for the blocking boto3 calls. asyncio.to_thread's default pool is min(32, cpus + 4),
    which on a 2-vCPU instance would cap Bedrock at 6 calls in flight whatever the semaphores say."""
    n = _pool_size(settings)
    with _client_lock:
        if n not in _executors:
            _executors[n] = ThreadPoolExecutor(max_workers=n, thread_name_prefix="bedrock")
        return _executors[n]


async def _in_thread(settings: Settings, fn: Callable[..., Any], *args: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor(settings), functools.partial(fn, *args))


def converse_request(model_id: str, system: str, user: str, tool: ToolSpec, max_tokens: int) -> dict:
    """The Converse kwargs: one tool in toolConfig and toolChoice forcing it."""
    req: dict[str, Any] = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": user}]}],
        "toolConfig": {
            "tools": [{"toolSpec": {"name": tool.name, "description": tool.description,
                                    "inputSchema": {"json": tool.input_schema}}}],
            "toolChoice": {"tool": {"name": tool.name}},
        },
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if system.strip():
        req["system"] = [{"text": system}]
    return req


def parse_reply(resp: dict, tool_name: str) -> Reply:
    """Pull the forced tool's input and the token usage out of a Converse response."""
    blocks = resp.get("output", {}).get("message", {}).get("content", []) or []
    tool_input: Any = None
    for block in blocks:
        use = block.get("toolUse") if isinstance(block, dict) else None
        if use and use.get("name") == tool_name:
            tool_input = use.get("input")
            break
    usage = resp.get("usage", {}) or {}
    return Reply(tool_input if isinstance(tool_input, dict) else {}, int(usage.get("inputTokens", 0)),
                 int(usage.get("outputTokens", 0)), str(resp.get("stopReason", "")))


def _converse_sync(settings: Settings, req: dict) -> dict:
    return _client(settings).converse(**req)


async def converse(settings: Settings, model_id: str, *, system: str, user: str, tool: ToolSpec,
                   max_tokens: int = 4096) -> Reply:
    """One Converse call forcing `tool`. Raises on any AWS error (the gateway turns that into a deny)."""
    if settings.fake_models:
        return await _fake_converse(system, user, tool)
    req = converse_request(model_id, system, user, tool, max_tokens)
    resp = await _in_thread(settings, _converse_sync, settings, req)
    return parse_reply(resp, tool.name)


# ---------------------------------------------------------------------------
# Guardrails tripwire
# ---------------------------------------------------------------------------


def chunk_text(text: str, size: int = GUARDRAIL_CHUNK_CHARS, overlap: int = GUARDRAIL_OVERLAP_CHARS) -> list[str]:
    """Overlapping slices of at most `size` characters. Empty text gives no chunks."""
    if not text:
        return []
    step = max(1, size - overlap)
    out = []
    for start in range(0, len(text), step):
        out.append(text[start:start + size])
        if start + size >= len(text):
            break
    return out


def _guardrail_sync(settings: Settings, chunk: str) -> dict:
    return _client(settings).apply_guardrail(
        guardrailIdentifier=settings.guardrail_id,
        guardrailVersion=settings.guardrail_version or "DRAFT",
        source="INPUT",
        content=[{"text": {"text": chunk}}],
    )


def guardrail_filters(resp: dict) -> list[str]:
    """Content filters that fired, as TYPE:CONFIDENCE strings."""
    out = []
    for assessment in resp.get("assessments", []) or []:
        for f in (assessment.get("contentPolicy") or {}).get("filters", []) or []:
            if f.get("action") == "BLOCKED" or f.get("detected"):
                out.append(f"{f.get('type', '?')}:{f.get('confidence', '?')}")
    return out


async def apply_guardrail(settings: Settings, text: str) -> dict:
    """ApplyGuardrail (source INPUT) on text, chunked. Stops at the first chunk that intervenes.

    Returns {"action": "GUARDRAIL_INTERVENED" | "NONE" | "SKIPPED", "detail", "chunks", "filters"}.
    """
    if settings.fake_models:
        if _fake_scan is None:
            return {"action": "SKIPPED", "detail": "FAKE_MODELS=1: guardrail not called", "chunks": 0, "filters": []}
        hit = bool(_fake_scan(text))
        return {"action": "GUARDRAIL_INTERVENED" if hit else "NONE", "chunks": 1,
                "filters": ["PROMPT_ATTACK:SIMULATED"] if hit else [],
                "detail": "Simulated scan (FAKE_MODELS=1)" + (": prompt attack" if hit else ": clean")}
    if not settings.guardrail_id:
        return {"action": "SKIPPED", "detail": "No guardrail configured", "chunks": 0, "filters": []}
    chunks = chunk_text(text)
    for i, chunk in enumerate(chunks, start=1):
        resp = await _in_thread(settings, _guardrail_sync, settings, chunk)
        if resp.get("action") == "GUARDRAIL_INTERVENED":
            filters = guardrail_filters(resp)
            return {"action": "GUARDRAIL_INTERVENED", "chunks": len(chunks), "filters": filters,
                    "detail": f"Bedrock Guardrails intervened on chunk {i} of {len(chunks)}: "
                              f"{', '.join(filters) or 'policy match'}"}
    return {"action": "NONE", "chunks": len(chunks), "filters": [], "detail": f"{len(chunks)} chunk(s) scanned, clean"}
