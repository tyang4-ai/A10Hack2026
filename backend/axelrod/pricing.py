"""Token cost in USD from Settings.prices (PRICES_JSON, USD per million tokens in and out).

The gateway passes the run's own price table, so tests can set prices without touching the cache.
An unknown model costs 0.0 here. The gateway refuses live calls to a model without a price, so the
budget is always metered.
"""
from __future__ import annotations

from collections.abc import Mapping

PriceTable = Mapping[str, Mapping[str, float]]


def _table(prices: PriceTable | None) -> PriceTable:
    if prices is not None:
        return prices
    from .config import get_settings

    return get_settings().prices


def has_price(model_id: str, prices: PriceTable | None = None) -> bool:
    """True when the table has both an input and an output price for model_id."""
    p = _table(prices).get(model_id)
    return bool(p) and "in" in p and "out" in p


def cost(model_id: str, tokens_in: int, tokens_out: int, prices: PriceTable | None = None) -> float:
    """USD for one call, rounded to 6 places (a millionth of a dollar is below any meter we show)."""
    p = _table(prices).get(model_id)
    if not p:
        return 0.0
    usd = tokens_in / 1_000_000 * float(p.get("in", 0.0)) + tokens_out / 1_000_000 * float(p.get("out", 0.0))
    return round(usd, 6)
