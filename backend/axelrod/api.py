"""FastAPI app: every route the mock answers, with the same paths, shapes, status codes and error envelope.

Stub: only /healthz answers, so a deploy can be smoke-tested. B-store-api replaces this module.
Run with `uvicorn backend.axelrod.api:app` from the repo root.
"""
from __future__ import annotations

from fastapi import FastAPI

from .contract import CONTRACT_VERSION, m

PREFIX = "/api/v1"

app = FastAPI(title="Axelrod API", version=CONTRACT_VERSION,
              description="Axelrod backend (stub until B-store-api lands).")


@app.get("/healthz", response_model=m.Health)
async def healthz():
    return m.Health(ok=True, version=CONTRACT_VERSION, mode="live")
