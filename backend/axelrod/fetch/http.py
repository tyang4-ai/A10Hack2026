"""Raw HTTP for the gateway (httpx): one request per call, no redirect following (the gateway decides
every hop), 60 s timeout and a 50 MB cap. Only gateway.py imports this module.
get_once is a suggested signature: B-gateway may change it."""
from __future__ import annotations

MAX_BYTES = 50 * 1024 * 1024
TIMEOUT_S = 60.0


async def get_once(url: str, *, user_agent: str, timeout_s: float = TIMEOUT_S,
                   max_bytes: int = MAX_BYTES) -> tuple[int, dict[str, str], bytes]:
    """One GET without following redirects. Returns (status, headers, body)."""
    raise NotImplementedError("B-gateway")
