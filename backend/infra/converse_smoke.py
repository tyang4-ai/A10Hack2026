"""One forced-tool Converse call per model. Prints only the tool input and token usage.

Run from the repo root:
    .venv/Scripts/python backend/infra/converse_smoke.py

Reads AWS_REGION, MODEL_LARGE and MODEL_SMALL from the environment (defaults below).
Credentials come from the environment, never from this script.
"""
import json
import os
import sys
import time

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODELS = {
    "large": os.environ.get("MODEL_LARGE", "us.anthropic.claude-sonnet-5-5"),
    "small": os.environ.get("MODEL_SMALL", "us.anthropic.claude-haiku-5-5"),
}

TOOL = {
    "toolSpec": {
        "name": "record_metric",
        "description": "Record one financial metric found in the text.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "value": {"type": "number"},
                    "unit": {"type": "string"},
                    "period": {"type": "string"},
                },
                "required": ["metric", "value", "unit", "period"],
            }
        },
    }
}

TEXT = (
    "Revenue (in millions of €) for fiscal year 2025 was 23,410, "
    "up 4.1% on a comparable basis."
)


def main() -> int:
    client = boto3.client(
        "bedrock-runtime",
        region_name=REGION,
        config=Config(retries={"mode": "adaptive", "max_attempts": 8}),
    )
    ok = True
    for size, model_id in MODELS.items():
        t0 = time.perf_counter()
        try:
            resp = client.converse(
                modelId=model_id,
                system=[{"text": "Extract exactly what the text says. Use the tool."}],
                messages=[{"role": "user", "content": [{"text": TEXT}]}],
                toolConfig={"tools": [TOOL], "toolChoice": {"tool": {"name": "record_metric"}}},
                inferenceConfig={"maxTokens": 300},
            )
        except Exception as exc:  # report the error class and message only
            ok = False
            print(f"{size} {model_id}: ERROR {type(exc).__name__}: {exc}")
            continue
        ms = int((time.perf_counter() - t0) * 1000)
        blocks = resp["output"]["message"]["content"]
        tool_input = next((b["toolUse"]["input"] for b in blocks if "toolUse" in b), None)
        u = resp["usage"]
        print(
            f"{size} {model_id}: stop={resp['stopReason']} in={u['inputTokens']} "
            f"out={u['outputTokens']} {ms}ms tool_input={json.dumps(tool_input, ensure_ascii=False)}"
        )
        if tool_input is None:
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
