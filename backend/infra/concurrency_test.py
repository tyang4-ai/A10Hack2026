"""Bedrock concurrency test: N parallel small-model calls, then M parallel large-model calls.

Every call is a short forced-tool Converse request with adaptive retries (the same client
config the gateway uses). Prints successes, failures, throttle responses seen (including the
ones adaptive retry absorbed), retries, latency p50/p95 and total tokens. Never prints
credentials or request bodies.

Run from the repo root:
    .venv/Scripts/python backend/infra/concurrency_test.py

Environment (defaults in brackets):
    AWS_REGION [us-east-1]
    MODEL_SMALL [us.anthropic.claude-haiku-5-5]   N_SMALL [50]
    MODEL_LARGE [us.anthropic.claude-sonnet-5-5]  N_LARGE [8]
"""
import collections
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_SMALL = os.environ.get("MODEL_SMALL", "us.anthropic.claude-haiku-5-5")
MODEL_LARGE = os.environ.get("MODEL_LARGE", "us.anthropic.claude-sonnet-5-5")
N_SMALL = int(os.environ.get("N_SMALL", "50"))
N_LARGE = int(os.environ.get("N_LARGE", "8"))

TOOL = {
    "toolSpec": {
        "name": "record_number",
        "description": "Record the number in the text.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {"value": {"type": "number"}, "label": {"type": "string"}},
                "required": ["value", "label"],
            }
        },
    }
}

_lock = threading.Lock()
_attempt_errors: collections.Counter = collections.Counter()


def _on_needs_retry(response=None, **_):
    # Runs before botocore's retry handler for every attempt. Count error codes only.
    if response is None:
        return None
    _http, parsed = response
    code = (parsed or {}).get("Error", {}).get("Code")
    if code:
        with _lock:
            _attempt_errors[code] += 1
    return None


def make_client(pool: int):
    client = boto3.client(
        "bedrock-runtime",
        region_name=REGION,
        config=Config(
            retries={"mode": "adaptive", "max_attempts": 8},
            max_pool_connections=pool,
            read_timeout=120,
        ),
    )
    client.meta.events.register_first("needs-retry.bedrock-runtime.Converse", _on_needs_retry)
    return client


def one_call(client, model_id: str, i: int) -> dict:
    t0 = time.perf_counter()
    try:
        resp = client.converse(
            modelId=model_id,
            messages=[
                {
                    "role": "user",
                    "content": [{"text": f"Order backlog was {1000 + i} million euros. Record it."}],
                }
            ],
            toolConfig={"tools": [TOOL], "toolChoice": {"tool": {"name": "record_number"}}},
            inferenceConfig={"maxTokens": 200},
        )
        ok = any("toolUse" in b for b in resp["output"]["message"]["content"])
        return {
            "ok": ok,
            "ms": (time.perf_counter() - t0) * 1000,
            "retries": resp["ResponseMetadata"].get("RetryAttempts", 0),
            "in": resp["usage"]["inputTokens"],
            "out": resp["usage"]["outputTokens"],
            "error": None if ok else "no toolUse block",
        }
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        return {"ok": False, "ms": (time.perf_counter() - t0) * 1000, "retries": None,
                "in": 0, "out": 0, "error": code}


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * len(s) + 0.5)) - 1))
    return s[k]


def run_batch(label: str, model_id: str, n: int) -> dict:
    _attempt_errors.clear()
    client = make_client(pool=max(10, n + 4))
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda i: one_call(client, model_id, i), range(n)))
    wall = time.perf_counter() - t0
    ok = [r for r in results if r["ok"]]
    lat = [r["ms"] for r in ok]
    summary = {
        "batch": label,
        "model": model_id,
        "calls": n,
        "ok": len(ok),
        "failed": n - len(ok),
        "final_errors": dict(collections.Counter(r["error"] for r in results if not r["ok"])),
        "attempt_errors": dict(_attempt_errors),  # throttles etc. seen on any attempt
        "calls_retried": sum(1 for r in ok if r["retries"]),
        "retries_total": sum(r["retries"] or 0 for r in ok),
        "p50_ms": round(statistics.median(lat)) if lat else None,
        "p95_ms": round(pct(lat, 95)) if lat else None,
        "max_ms": round(max(lat)) if lat else None,
        "wall_s": round(wall, 1),
        "tokens_in": sum(r["in"] for r in results),
        "tokens_out": sum(r["out"] for r in results),
    }
    print(json.dumps(summary))
    return summary


def main() -> int:
    a = run_batch("small", MODEL_SMALL, N_SMALL)
    b = run_batch("large", MODEL_LARGE, N_LARGE)
    return 0 if a["failed"] == 0 and b["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
