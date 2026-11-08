"""Raw HTTP for the gateway (httpx): one request per call, no redirect following (the gateway decides
every hop), 60 s timeout and a 50 MB cap. Only gateway.py imports this module.

split_url parses with httpx.URL, the same parser the request uses, so the host and path the gateway
decides on are the host and path that get requested.
"""
from __future__ import annotations

import asyncio

import httpx

MAX_BYTES = 50 * 1024 * 1024
TIMEOUT_S = 60.0


class TooLarge(Exception):
    """The response body is over the cap. The gateway turns it into a deny."""


def split_url(url: str) -> tuple[str, str, str] | None:
    """(scheme, host, path) for an http(s) URL, or None when it isn't one we would ever fetch.

    Paths with dot segments are refused, so a prefix rule can't be walked out of.
    """
    try:
        u = httpx.URL(url)
    except Exception:
        return None
    scheme, host = u.scheme.lower(), (u.host or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        return None
    path = u.path or "/"
    if "/../" in path or path.endswith("/..") or "/./" in path:
        return None
    return scheme, host, path


async def get_once(url: str, *, user_agent: str, timeout_s: float = TIMEOUT_S,
                   max_bytes: int = MAX_BYTES,
                   transport: httpx.AsyncBaseTransport | None = None) -> tuple[int, dict[str, str], bytes]:
    """One GET without following redirects. Returns (status, lowercased headers, body).

    timeout_s bounds the whole request, not each read. transport is for tests (httpx.MockTransport).
    """

    async def _get() -> tuple[int, dict[str, str], bytes]:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout_s, transport=transport,
                                     headers={"User-Agent": user_agent}) as client:
            async with client.stream("GET", url) as resp:
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise TooLarge(f"Content-Length {declared} is over the {max_bytes} byte cap")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise TooLarge(f"body is over the {max_bytes} byte cap")
                    chunks.append(chunk)
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status_code, headers, b"".join(chunks)

    return await asyncio.wait_for(_get(), timeout_s)
