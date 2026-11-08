"""Token cost in USD from Settings.prices (PRICES_JSON, USD per million tokens in and out)."""
from __future__ import annotations


def cost(model_id: str, tokens_in: int, tokens_out: int) -> float:
    raise NotImplementedError("B-gateway")
