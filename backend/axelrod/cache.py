"""Document cache: the key, the layout and the store (read a hit, store a miss).

The demo serves fetch bodies from S3 so it is fast and repeatable. The gateway still decides every request
first; only an allowed fetch may read the cache, and a blocked request never touches it. A document is found
by the sha256 of its normalized URL:

    cache/docs/<key>.bin     the body, byte for byte
    cache/docs/<key>.json    CacheMeta: url, normalized_url, content_type, bytes, sha256, collected_at,
                             origin ("archive" or "cache") and, for archive files, source_path

in the vault bucket (aws mode), or under DATA_DIR/cache/docs/ (local mode). Gordon's summer 2026 archive
lives under archive/shl/<relative path> plus archive/shl/manifest.json, and every archive file is also
indexed in cache/docs/ under its original URL, or under its s3:// URL when no original URL is known.
V4-archive's uploader uses normalize_url, doc_key and this layout, so the keys it writes are the keys the
gateway reads.

The cache never fails a request: a read error is a miss and a write error is skipped, each logged once.
A missing S3 key answers 403 instead of 404 when the caller lacks s3:ListBucket, so 403 is a miss too.
Every read checks the body against the stored sha256 and the URL against the stored one, so a bad entry
is a miss, never a wrong document.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import BaseModel, ValidationError

from .ids import now_iso

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

CACHE_PREFIX = "cache/docs/"
ARCHIVE_PREFIX = "archive/shl/"
ARCHIVE_MANIFEST = ARCHIVE_PREFIX + "manifest.json"
BODY_SUFFIX = ".bin"
META_SUFFIX = ".json"
DEFAULT_PORTS = {"http": 80, "https": 443}
S3_SCHEME = "s3"
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound", "403", "AccessDenied", "Forbidden"})

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
    # V2-gateway: a stored redirect keeps its status and Location (empty body), so a replay decides every
    # hop again. Archive entries never set these, so they read as a plain 200.
    http_status: int = 200
    location: str | None = None


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
    """Run.cache_mode for a new run: "documents" when CACHE_DOCUMENTS is on (the gateway serves fetch
    bodies from the cache), else "off". Model answers are not replayed yet (V8-fork), so a run never claims
    "documents_and_models" whatever CACHE_MODELS says."""
    return "documents" if settings.cache_documents else "off"


# ---------------------------------------------------------------------------
# s3:// archive URLs
# ---------------------------------------------------------------------------


def split_s3_url(url: str) -> tuple[str, str, str] | None:
    """("s3", bucket, "/key") for s3://bucket/key, the way the policy sees an archive copy (domain = bucket).
    None for anything else. Dot segments are refused, as for http(s), so a prefix rule can't be walked out of."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() != S3_SCHEME or not parts.netloc or "@" in parts.netloc or ":" in parts.netloc:
        return None
    path = parts.path or "/"
    decoded = unquote(path)
    for p in (path, decoded):
        if "/../" in p or p.endswith("/..") or "/./" in p:
            return None
    return S3_SCHEME, parts.netloc.lower().rstrip("."), path


def is_s3_url(url: str) -> bool:
    """An archive copy with no known original URL: it can only ever be served from the cache."""
    return split_s3_url(url) is not None


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclass
class CachedDoc:
    """A hit: the stored body and its metadata."""

    meta: CacheMeta
    body: bytes


def _error_code(e: Exception) -> str:
    """botocore ClientError code ("NoSuchKey", "403", ...), or "" for anything else."""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        return str(resp.get("Error", {}).get("Code", ""))
    return ""


class DocCache:
    """Read a hit, store a miss. aws mode reads and writes the vault bucket, local mode DATA_DIR/cache/docs/.

    Every method swallows its own errors (logged once per kind), so the gateway can call it on any allowed
    fetch without risking the request. Sync methods are for threads; the gateway uses aget and aput.
    """

    def __init__(self, settings: Settings, *, s3: Any = None, mode: Literal["local", "aws"] | None = None) -> None:
        self.settings = settings
        self.mode = mode or settings.store
        self._s3 = s3
        self._logged: set[str] = set()
        self._lock = threading.Lock()

    # -- plumbing ---------------------------------------------------------------------------

    @property
    def aws(self) -> bool:
        return self.mode == "aws"

    def s3(self) -> Any:
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3", region_name=self.settings.aws_region)
        return self._s3

    def describe(self) -> str:
        if self.aws:
            return f"s3://{self.settings.vault_bucket}/{CACHE_PREFIX}"
        return str(local_path(self.settings, CACHE_PREFIX))

    def _log_once(self, kind: str, text: str) -> None:
        with self._lock:
            if kind in self._logged:
                return
            self._logged.add(kind)
        log.warning("document cache (%s): %s", self.describe(), text)

    def _read(self, object_key: str) -> bytes | None:
        """The object's bytes, or None when it is missing. 403 counts as missing (no s3:ListBucket)."""
        if not self.aws:
            p = local_path(self.settings, object_key)
            return p.read_bytes() if p.is_file() else None
        bucket = self.settings.vault_bucket
        if not bucket:
            raise RuntimeError("VAULT_BUCKET is not set, so the S3 document cache has nowhere to look")
        try:
            return self.s3().get_object(Bucket=bucket, Key=object_key)["Body"].read()
        except Exception as e:  # noqa: BLE001  botocore ClientError, or a fake client's KeyError
            code = _error_code(e)
            if isinstance(e, KeyError) or code in MISSING_CODES:
                if code in ("403", "AccessDenied", "Forbidden"):
                    self._log_once("403", f"S3 answered {code} for {object_key}: treated as a miss "
                                          "(the role likely lacks s3:ListBucket, so a missing key looks forbidden)")
                return None
            raise

    def _write(self, object_key: str, data: bytes, content_type: str) -> None:
        if not self.aws:
            p = local_path(self.settings, object_key)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)
            return
        bucket = self.settings.vault_bucket
        if not bucket:
            raise RuntimeError("VAULT_BUCKET is not set, so the S3 document cache has nowhere to write")
        sha = hashlib.sha256(data).hexdigest()
        self.s3().put_object(Bucket=bucket, Key=object_key, Body=data, ContentType=content_type or
                             "application/octet-stream", Metadata={"sha256": sha})

    # -- read -------------------------------------------------------------------------------

    def meta(self, url: str) -> CacheMeta | None:
        """The stored metadata for url, or None (missing, unreadable or not about this URL)."""
        key = doc_key(url)
        try:
            raw = self._read(meta_key(key))
            if raw is None:
                return None
            meta = CacheMeta.model_validate(json.loads(raw))
        except (ValueError, ValidationError) as e:
            self._log_once("bad-meta", f"unreadable metadata {meta_key(key)} ({type(e).__name__}): treated as a miss")
            return None
        except Exception as e:  # noqa: BLE001  never fail the request because of the cache
            self._log_once(f"read-{type(e).__name__}", f"read failed ({type(e).__name__}: {e}): treated as a miss")
            return None
        if meta.normalized_url != normalize_url(url):
            self._log_once("url-mismatch", f"{meta_key(key)} is about {meta.normalized_url}, not {normalize_url(url)}")
            return None
        return meta

    def get(self, url: str, *, max_bytes: int | None = None) -> CachedDoc | None:
        """The cached body for url, checked against its sha256, or None for a miss. Never raises."""
        meta = self.meta(url)
        if meta is None:
            return None
        if max_bytes is not None and meta.bytes > max_bytes:
            self._log_once("too-large", f"{url} is {meta.bytes} bytes, over the {max_bytes} byte cap: a miss")
            return None
        try:
            body = self._read(body_key(doc_key(url)))
        except Exception as e:  # noqa: BLE001
            self._log_once(f"read-{type(e).__name__}", f"read failed ({type(e).__name__}: {e}): treated as a miss")
            return None
        if body is None:
            self._log_once("no-body", f"{url} has metadata but no body: treated as a miss")
            return None
        if len(body) != meta.bytes or hashlib.sha256(body).hexdigest() != meta.sha256:
            self._log_once("sha-mismatch", f"{url}: the body does not match its stored sha256: treated as a miss")
            return None
        return CachedDoc(meta=meta, body=body)

    # -- write ------------------------------------------------------------------------------

    def put(self, url: str, body: bytes, content_type: str, *, http_status: int = 200,
            location: str | None = None) -> CacheMeta | None:
        """Store a live fetch's response (origin "cache", collected now), body before metadata, so metadata
        only ever points at a body that is there. An existing entry is kept, never overwritten (the archive
        wins). Returns the stored metadata, or None when nothing was written. Never raises."""
        if not storable(http_status, location):
            return None
        try:
            if self.meta(url) is not None:
                return None
            key = doc_key(url)
            meta = CacheMeta(url=url, normalized_url=normalize_url(url), content_type=content_type or "",
                             bytes=len(body), sha256=hashlib.sha256(body).hexdigest(), collected_at=now_iso(),
                             origin="cache", http_status=http_status,
                             location=location if http_status in REDIRECT_CODES else None)
            self._write(body_key(key), body, content_type)
            self._write(meta_key(key), (meta.model_dump_json(indent=2) + "\n").encode("utf-8"), "application/json")
            return meta
        except Exception as e:  # noqa: BLE001  a failed copy never fails the request
            self._log_once(f"write-{type(e).__name__}", f"write failed ({type(e).__name__}: {e}): not cached")
            return None

    # -- async ------------------------------------------------------------------------------

    async def aget(self, url: str, *, max_bytes: int | None = None) -> CachedDoc | None:
        """get off the event loop (S3 in aws mode, disk in local mode)."""
        return await asyncio.to_thread(self.get, url, max_bytes=max_bytes)

    async def aput(self, url: str, body: bytes, content_type: str, *, http_status: int = 200,
                   location: str | None = None) -> CacheMeta | None:
        return await asyncio.to_thread(self.put, url, body, content_type, http_status=http_status, location=location)


def storable(http_status: int, location: str | None) -> bool:
    """Only a 2xx body or a redirect with a Location is worth replaying. Errors are always fetched again."""
    return 200 <= http_status < 300 or (http_status in REDIRECT_CODES and bool(location))
