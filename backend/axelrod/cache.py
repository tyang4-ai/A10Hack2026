"""Document cache: the key and the layout (real since V1-shapes), the store itself comes with V2-gateway.

The demo serves fetch bodies from S3 so it is fast and repeatable. The gateway still decides every request
first; only an allowed fetch may read the cache. A document is found by the sha256 of its normalized URL:

    cache/docs/<key>.bin     the body, byte for byte
    cache/docs/<key>.json    CacheMeta: url, normalized_url, content_type, bytes, sha256, collected_at,
                             origin ("archive" or "cache") and, for archive files, source_path

in the vault bucket (aws mode), or under DATA_DIR/cache/docs/ (local mode). Gordon's summer 2026 archive
lives under archive/shl/<relative path> plus archive/shl/manifest.json, and every archive file is also
indexed in cache/docs/ under its original URL, or under its s3:// URL when no original URL is known.

V2-gateway adds the cache store here (read a hit, store a miss). V4-archive's uploader uses normalize_url,
doc_key and the layout below, so the keys it writes are the keys the gateway reads.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

if TYPE_CHECKING:
    from .config import Settings

CACHE_PREFIX = "cache/docs/"
ARCHIVE_PREFIX = "archive/shl/"
ARCHIVE_MANIFEST = ARCHIVE_PREFIX + "manifest.json"
BODY_SUFFIX = ".bin"
META_SUFFIX = ".json"
DEFAULT_PORTS = {"http": 80, "https": 443}

CacheOrigin = Literal["archive", "cache"]


class CacheMeta(BaseModel):
    """The metadata stored next to a cached body (cache/docs/<key>.json)."""

    url: str                        # the URL as first requested (or the archive file's original URL)
    normalized_url: str             # normalize_url(url): what the key is the sha256 of
    content_type: str               # the HTTP Content-Type, or the archive file's type
    bytes: int
    sha256: str                     # of the body
    collected_at: str               # when the bytes were first collected (ISO 8601 UTC)
    origin: CacheOrigin             # archive = collected before the event, cache = stored by an Axelrod run
    source_path: str | None = None  # archive files: the path relative to the archive root


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def _split_host_port(hostport: str) -> tuple[str, str]:
    """("host", "port") from "host:port", "[v6]:port" or "host". The port is "" when absent or empty."""
    if hostport.startswith("["):
        end = hostport.find("]")
        if end != -1:
            host, rest = hostport[:end + 1], hostport[end + 1:]
            return host, rest[1:] if rest.startswith(":") else ""
        return hostport, ""
    host, colon, port = hostport.rpartition(":")
    if colon and (port == "" or port.isdigit()):
        return host, port
    return hostport, ""


def _netloc(scheme: str, netloc: str) -> str:
    """Lowercase host, default port dropped, user info kept as written."""
    userinfo, at, hostport = netloc.rpartition("@")
    host, port = _split_host_port(hostport)
    if port and DEFAULT_PORTS.get(scheme) == int(port):
        port = ""
    return f"{userinfo}{at}{host.lower()}{':' + port if port else ''}"


def normalize_url(url: str) -> str:
    """The cache's view of a URL: lowercase scheme and host, no fragment, no default port (80 for http,
    443 for https). Path and query are kept exactly, since servers may treat their case and order as
    meaningful. A string with no scheme or host only loses its fragment and outer whitespace."""
    raw = url.strip()
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw.split("#", 1)[0]
    scheme = parts.scheme.lower()
    return urlunsplit((scheme, _netloc(scheme, parts.netloc), parts.path, parts.query, ""))


def doc_key(url: str) -> str:
    """sha256 hex of the normalized URL: the file name of a cached document."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def body_key(key: str) -> str:
    """Object key of a cached body in the vault bucket: cache/docs/<key>.bin."""
    return f"{CACHE_PREFIX}{key}{BODY_SUFFIX}"


def meta_key(key: str) -> str:
    """Object key of a cached body's metadata: cache/docs/<key>.json."""
    return f"{CACHE_PREFIX}{key}{META_SUFFIX}"


def archive_key(relative_path: str) -> str:
    """Object key of an archive file: archive/shl/<relative path>, with forward slashes."""
    return ARCHIVE_PREFIX + relative_path.replace("\\", "/").lstrip("/")


def local_path(settings: Settings, object_key: str) -> Path:
    """Where local mode keeps an object: the same key under DATA_DIR."""
    from .store.vault import data_root  # lazy: the store package imports this module

    return data_root(settings) / object_key


def run_cache_mode(settings: Settings) -> str:
    """Run.cache_mode for a new run. "off" until the caches serve requests (V2-gateway for documents,
    V8-fork for model answers), whatever CACHE_DOCUMENTS and CACHE_MODELS say, so a run never claims a
    cache it did not use."""
    return "off"
