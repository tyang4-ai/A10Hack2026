"""Import Gordon's summer 2026 SHL archive (primary source documents only) into the vault bucket.

Usage (from the repo root):
  PY backend/scripts/import_archive.py --dry-run                 # counts and a manifest sample, writes nothing
  PY backend/scripts/import_archive.py --upload                  # boto3 to s3://<VAULT_BUCKET>/
  PY backend/scripts/import_archive.py --upload --target local   # the same layout under DATA_DIR (local mode)

What it opens in the archive, read only, and nothing else: the primary files in the included folders (hashed
and sniffed for their content type, never parsed), the `.meta.md` sidecar beside each one, and
`00-Meta/_document-index.md` plus `00-Meta/manifest.json`, only for each file's original URL and collection date.
It never opens, copies or runs a script or note from the archive.

What --upload writes (layout from backend/PLANS.md, "v2 shared interfaces"):
  archive/shl/<relative path>           the file's bytes
  archive/shl/manifest.json             path, original URL or null, sha256, bytes, content type, collected_at
  cache/docs/<key>.bin and <key>.json   the document cache entry (origin "archive"), key = cache.doc_key(url)
A file with no known original URL (or one that shares its URL with different bytes) is indexed under its
s3:// URL. Idempotent: an object whose sha256 already matches is skipped, and an existing cache entry with
other bytes (a live fetch from tonight) is kept, never overwritten, unless --replace-cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so `backend.axelrod` imports

from backend.axelrod import cache  # noqa: E402  (normalize_url, doc_key, CacheMeta and the layout)

DEFAULT_ROOT = Path(r"B:\Alioth\Notes\Finance\siemens-healthineers")
INCLUDE_DIRS = ("01-Annual-Reports", "02-Quarterly-Reports", "03-Other-Company-Reports", "05-Regulatory-and-Legal",
                "07-Predecessor-Siemens-AG", "08-Competitors", "10-Procurement-Evidence", "Varian")
# Gordon's own work, plus the folders the include list leaves out, at any depth (Varian/ mirrors the top layout).
EXCLUDE_DIRS = {"09-My-Analysis", "11-Technical-Explainers", "12-Verification", "data",
                "04-News-Coverage", "06-Macro-Political-Social"}
PRIMARY_EXT = {".pdf", ".htm", ".html", ".csv", ".json", ".xml", ".txt"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".tif", ".tiff"}
METADATA_JSON = {"manifest.json", "index.json", "metadata.json"}
SIDECAR = ".meta.md"
INDEX_FILE = "00-Meta/_document-index.md"
MANIFEST_FILE = "00-Meta/manifest.json"
NO_REDISTRIBUTE = "do not redistribute"
HTTP_URL = re.compile(r"https?://\S+", re.IGNORECASE)
LABEL = "collected before the event (summer 2026 archive), primary source documents only"

ARCHIVE_PREFIX = cache.ARCHIVE_PREFIX       # archive/shl/
MANIFEST_KEY = cache.ARCHIVE_MANIFEST        # archive/shl/manifest.json

# Exclusion reasons that are bulk housekeeping: counted, never listed path by path.
BULK_REASONS = ("sidecar", "markdown note", "image", "asset or index folder")
EXAMPLES = 6        # paths the dry run lists per other exclusion reason


def _open(path: Path):
    """Every read of an archive file goes through here (tests check what gets opened)."""
    return open(path, "rb")


# -- what to include ----------------------------------------------------------------------------------------------

@dataclass
class ArchiveFile:
    path: str                       # relative to the archive root, forward slashes
    abs_path: Path
    bytes: int = 0
    sha256: str = ""
    content_type: str = ""
    url: str | None = None          # the original URL, None when unknown
    url_from: str = "none"          # sidecar | manifest | none
    collected_at: str | None = None
    cache_url: str = ""             # the URL the cache indexes it under (url, or its s3:// URL)
    cache_key: str = ""
    writes_cache: bool = True       # False when another file with the same URL and bytes owns the entry
    recorded_sha256: str | None = None   # what the sidecar says, compared with the bytes on disk
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportPlan:
    root: Path
    bucket: str
    files: list[ArchiveFile]
    excluded: list[tuple[str, str]]          # (path, reason)

    def manifest(self) -> dict[str, Any]:
        """archive/shl/manifest.json. Deterministic (no timestamp), so a rerun with the same input skips it."""
        return {
            "archive": "shl",
            "label": LABEL,
            "prefix": ARCHIVE_PREFIX,
            "files": len(self.files),
            "bytes": sum(f.bytes for f in self.files),
            "entries": [manifest_entry(f) for f in self.files],
            "excluded": dict(sorted(Counter(r for _, r in self.excluded).items())),
        }


def manifest_entry(f: ArchiveFile) -> dict[str, Any]:
    return {"path": f.path, "url": f.url, "sha256": f.sha256, "bytes": f.bytes, "content_type": f.content_type,
            "collected_at": f.collected_at, "cache_url": f.cache_url, "cache_key": f.cache_key}


def exclusion_reason(rel: str, siblings: set[str]) -> str | None:
    """Why a file in an included folder is not a primary source, or None when it may be one."""
    p = PurePosixPath(rel)
    for part in p.parts[:-1]:
        if part in EXCLUDE_DIRS:
            return f"excluded folder {part}/"
        if part.startswith(("_", ".")):
            return "asset or index folder"
    name, ext = p.name, p.suffix.lower()
    if name.endswith(SIDECAR):
        return "sidecar"
    if name.startswith(("_", ".")):
        return "index or hidden file"
    if ext == ".md":
        return "markdown note"
    if ext == ".docx":
        return "docx"
    if ext in IMAGE_EXT:
        return "image"
    if ext == ".zip":
        return "zip"
    if ext not in PRIMARY_EXT:
        return f"not a primary type ({ext or 'no extension'})"
    if ext == ".json" and _is_metadata_json(name, siblings):
        return "json metadata about another file"
    return None


def _is_metadata_json(name: str, siblings: set[str]) -> bool:
    """x.pdf.json, x.json next to x.pdf, *.meta.json and manifest-like names describe other files."""
    lower = name.lower()
    if lower in METADATA_JSON or lower.endswith((".meta.json", "-meta.json", "_meta.json")):
        return True
    stem = name[: -len(".json")]
    if stem in siblings:
        return True
    return any(s != name and PurePosixPath(s).stem == stem and PurePosixPath(s).suffix.lower() in PRIMARY_EXT - {".json"}
               for s in siblings)


# -- provenance: sidecars, the document index, manifest.json --------------------------------------------------------

def parse_frontmatter(text: str) -> dict[str, str]:
    """Top-level `key: value` pairs of a YAML front matter block. Enough for the sidecars, with no YAML dependency."""
    text = text.replace("\r\n", "\n").lstrip("\ufeff")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^([A-Za-z][\w-]*):[ \t]*(.*)$", line)
        if m:
            out[m.group(1)] = _unquote(m.group(2).strip())
    return out


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def read_sidecar(path: Path) -> dict[str, str] | None:
    sidecar = path.with_name(path.name + SIDECAR)
    if not sidecar.is_file():
        return None
    with _open(sidecar) as fh:
        return parse_frontmatter(fh.read().decode("utf-8", errors="replace"))


def read_index(root: Path) -> dict[str, str]:
    """relative path -> retrieval date (YYYY-MM-DD) from 00-Meta/_document-index.md."""
    path = root / INDEX_FILE
    if not path.is_file():
        return {}
    with _open(path) as fh:
        lines = fh.read().decode("utf-8", errors="replace").splitlines()
    out: dict[str, str] = {}
    top = sub = ""
    for line in lines:
        if line.startswith("## "):
            top, sub = "", ""
        elif m := re.match(r"^`([^`]+/)`\s", line):
            top, sub = m.group(1), ""
        elif m := re.match(r"^###\s+`([^`]+/)`", line):
            sub = m.group(1)
        elif (m := re.match(r"^\|\s*`([^`]+)`\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|", line)) and top:
            out[top + sub + m.group(1)] = m.group(2)
    return out


def read_manifest_urls(root: Path) -> dict[str, set[str]]:
    """URL basename -> URLs named in 00-Meta/manifest.json, a fallback for files without a sidecar."""
    path = root / MANIFEST_FILE
    if not path.is_file():
        return {}
    with _open(path) as fh:
        try:
            data = json.loads(fh.read().decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return {}
    out: dict[str, set[str]] = defaultdict(set)
    for url in _urls_in(data):
        name = PurePosixPath(urlsplit(url).path).name
        if name:
            out[name].add(url)
    return out


def _urls_in(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        for v in node.values():
            yield from _urls_in(v)
    elif isinstance(node, list):
        for v in node:
            yield from _urls_in(v)
    elif isinstance(node, str) and HTTP_URL.fullmatch(node):
        yield node


def _url(value: str | None) -> str | None:
    """A sidecar's source-url, if it is one ("n/a, supplied as a file bundle" is not)."""
    if value and HTTP_URL.fullmatch(value.strip()):
        return value.strip()
    return None


def _iso_date(value: str | None) -> str | None:
    """YYYY-MM-DD (the retrieval date) as the contract's timestamp, midnight UTC."""
    if value and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return f"{value.strip()}T00:00:00.000Z"
    return None


# -- bytes: hash and content type ---------------------------------------------------------------------------------

def hash_and_sniff(path: Path) -> tuple[str, int, bytes]:
    """(sha256 hex, size, first 2 KB). One pass over the file."""
    h = hashlib.sha256()
    size, head = 0, b""
    with _open(path) as fh:
        while chunk := fh.read(1 << 20):
            if not head:
                head = chunk[:2048]
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size, head


def sniff(head: bytes, ext: str) -> tuple[str | None, str | None]:
    """(content type, problem). A problem means the bytes are not the document the name promises."""
    if not head:
        return None, "empty file"
    body = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if b"%PDF-" in head[:1024]:
        return "application/pdf", None
    if ext == ".pdf":
        return None, "named .pdf but the bytes are not a PDF (likely an error page)"
    if ext == ".json":
        return ("application/json", None) if body[:1] in (b"{", b"[") else (None, "named .json but not JSON")
    if ext in (".htm", ".html"):
        return "text/html", None
    if ext == ".xml":
        return "application/xml", None
    if ext == ".csv":
        return "text/csv", None
    return "text/plain", None


# -- the plan -------------------------------------------------------------------------------------------------------

def s3_url(bucket: str, rel: str) -> str:
    """The URL of the archived copy, for files with no known original URL."""
    return f"s3://{bucket}/{quote(cache.archive_key(rel), safe='/()+')}"


def walk(root: Path) -> Iterable[tuple[str, Path, set[str]]]:
    """(relative path, absolute path, sibling names) for every file under the included folders, sorted.
    Lists folders only: nothing outside INCLUDE_DIRS is ever touched."""
    siblings: dict[Path, set[str]] = {}
    for top in INCLUDE_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            if path.parent not in siblings:
                siblings[path.parent] = {s.name for s in path.parent.iterdir()}
            yield path.relative_to(root).as_posix(), path, siblings[path.parent]


def build_plan(root: Path, bucket: str, *, include_licensed: bool = False) -> ImportPlan:
    """Decide what goes in, with provenance, hashes and cache keys. Reads only; writes nothing."""
    index = read_index(root)
    manifest_urls = read_manifest_urls(root)
    files: list[ArchiveFile] = []
    excluded: list[tuple[str, str]] = []
    for rel, path, siblings in walk(root):
        reason = exclusion_reason(rel, siblings)
        if reason is None:
            f = ArchiveFile(path=rel, abs_path=path)
            reason = _provenance(f, read_sidecar(path), index, manifest_urls, include_licensed) or _fill_bytes(f)
        if reason:
            excluded.append((rel, reason))
        else:
            files.append(f)
    assign_cache_urls(files, bucket)
    return ImportPlan(root=root, bucket=bucket, files=files, excluded=excluded)


def _provenance(f: ArchiveFile, meta: dict[str, str] | None, index: dict[str, str],
                manifest_urls: dict[str, set[str]], include_licensed: bool) -> str | None:
    """Fill url and collected_at. Returns an exclusion reason for files with no provenance or no right to share."""
    if meta is None:
        if f.path not in index:
            return "no .meta.md sidecar and not in _document-index.md (no provenance, may be derived data)"
        f.collected_at = _iso_date(index[f.path])
        found = manifest_urls.get(PurePosixPath(f.path).name, set())
        if len(found) == 1:
            f.url, f.url_from = next(iter(found)), "manifest"
        return None
    rights = meta.get("rights", "")
    if NO_REDISTRIBUTE in rights.lower() and not include_licensed:
        return "licensed third-party content (sidecar rights: do not redistribute)"
    f.url = _url(meta.get("source-url"))
    f.url_from = "sidecar" if f.url else "none"
    f.collected_at = _iso_date(meta.get("retrieved-date")) or _iso_date(index.get(f.path))
    f.recorded_sha256 = (meta.get("sha256") or "").lower() or None
    return None


def _fill_bytes(f: ArchiveFile) -> str | None:
    """Hash and sniff the file. Returns an exclusion reason when the bytes are not what the name promises."""
    f.sha256, f.bytes, head = hash_and_sniff(f.abs_path)
    ext = PurePosixPath(f.path).suffix.lower()
    ctype, problem = sniff(head, ext)
    if problem:
        return problem
    f.content_type = ctype or "application/octet-stream"
    if ctype == "application/pdf" and ext != ".pdf":
        f.warnings.append(f"PDF bytes under a {ext} name")
    if f.recorded_sha256 and f.recorded_sha256 != f.sha256:
        f.warnings.append("sha256 differs from the sidecar (file changed after collection)")
    return None


def assign_cache_urls(files: list[ArchiveFile], bucket: str) -> None:
    """One cache entry per normalized URL. Several files on one URL share it only when their bytes match."""
    groups: dict[str, list[ArchiveFile]] = defaultdict(list)
    for f in files:
        if f.url:
            groups[cache.normalize_url(f.url)].append(f)
        else:
            f.cache_url = s3_url(bucket, f.path)
    for group in groups.values():
        if len({f.sha256 for f in group}) == 1:
            for i, f in enumerate(group):
                f.cache_url, f.writes_cache = group[0].url or "", i == 0
            continue
        for f in group:
            f.cache_url = s3_url(bucket, f.path)
            f.warnings.append(f"original URL shared by {len(group)} files with different bytes: indexed under s3://")
    for f in files:
        f.cache_key = cache.doc_key(f.cache_url)
        if f.writes_cache and not f.collected_at:
            f.writes_cache = False   # CacheMeta needs collected_at: archived, but not served from the cache
            f.warnings.append("no collection date: archived but not indexed in the cache")


def cache_meta(f: ArchiveFile) -> cache.CacheMeta:
    """cache/docs/<key>.json for an archive file. source_path is relative to the archive root."""
    return cache.CacheMeta(url=f.cache_url, normalized_url=cache.normalize_url(f.cache_url),
                           content_type=f.content_type, bytes=f.bytes, sha256=f.sha256,
                           collected_at=f.collected_at or "", origin="archive", source_path=f.path)


# -- targets: the vault bucket, or its local mirror -----------------------------------------------------------------

class S3Target:
    """The vault bucket. Object metadata `sha256` is what makes reruns skip."""

    def __init__(self, client: Any, bucket: str) -> None:
        self.client, self.bucket = client, bucket

    def stored_sha(self, key: str) -> str | None:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as e:  # noqa: BLE001
            if _missing(e):
                return None
            raise
        return (head.get("Metadata") or {}).get("sha256")

    def put_file(self, key: str, path: Path, content_type: str, sha: str) -> None:
        self.client.upload_file(str(path), self.bucket, key,
                                ExtraArgs={"ContentType": content_type, "Metadata": {"sha256": sha}})

    def put_bytes(self, key: str, data: bytes, content_type: str, sha: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                               Metadata={"sha256": sha})

    def copy(self, src: str, dst: str, content_type: str, sha: str) -> None:
        self.client.copy_object(Bucket=self.bucket, Key=dst, CopySource={"Bucket": self.bucket, "Key": src},
                                MetadataDirective="REPLACE", ContentType=content_type, Metadata={"sha256": sha})

    def get_json(self, key: str) -> dict | None:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as e:  # noqa: BLE001
            if _missing(e):
                return None
            raise
        return json.loads(body)

    def describe(self) -> str:
        return f"s3://{self.bucket}/"


def _missing(e: Exception) -> bool:
    code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
    return code in ("404", "NoSuchKey", "NotFound")


class LocalTarget:
    """The same layout under DATA_DIR (cache.local_path), for STORE=local runs."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.root = cache.local_path(settings, "")

    def _p(self, key: str) -> Path:
        return cache.local_path(self.settings, key)

    def stored_sha(self, key: str) -> str | None:
        p = self._p(key)
        if not p.is_file():
            return None
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def _write(self, key: str, write: Callable[[Path], None]) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        write(tmp)
        tmp.replace(p)

    def put_file(self, key: str, path: Path, content_type: str, sha: str) -> None:
        self._write(key, lambda tmp: shutil.copyfile(path, tmp))

    def put_bytes(self, key: str, data: bytes, content_type: str, sha: str) -> None:
        self._write(key, lambda tmp: tmp.write_bytes(data))

    def copy(self, src: str, dst: str, content_type: str, sha: str) -> None:
        self._write(dst, lambda tmp: shutil.copyfile(self._p(src), tmp))

    def get_json(self, key: str) -> dict | None:
        p = self._p(key)
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    def describe(self) -> str:
        return f"{self.root} (local mirror)"


# -- upload ---------------------------------------------------------------------------------------------------------

def _json_bytes(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def upload_one(target: Any, f: ArchiveFile, *, replace_cache: bool = False) -> list[str]:
    """The archive object, then (if this file owns it) the cache entry, body before metadata."""
    outcomes = []
    key = cache.archive_key(f.path)
    if target.stored_sha(key) == f.sha256:
        outcomes.append("archive skipped")
    else:
        target.put_file(key, f.abs_path, f.content_type, f.sha256)
        outcomes.append("archive uploaded")
    if not f.writes_cache:
        return outcomes
    meta_key = cache.meta_key(f.cache_key)
    existing = target.get_json(meta_key)
    if existing is not None and existing.get("sha256") == f.sha256:
        outcomes.append("cache skipped")
    elif existing is not None and not replace_cache:
        outcomes.append(f"cache conflict: kept the existing entry (origin {existing.get('origin')})")
    else:
        target.copy(key, cache.body_key(f.cache_key), f.content_type, f.sha256)
        body = _json_bytes(cache_meta(f).model_dump(mode="json"))
        target.put_bytes(meta_key, body, "application/json", hashlib.sha256(body).hexdigest())
        outcomes.append("cache written")
    return outcomes


def upload(plan: ImportPlan, target: Any, *, workers: int = 8, replace_cache: bool = False,
           log: Callable[[str], None] = print) -> dict[str, Any]:
    """Every file, then the manifest last, so its presence means the files are there."""
    counts: Counter[str] = Counter()
    conflicts: list[str] = []
    errors: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(upload_one, target, f, replace_cache=replace_cache): f for f in plan.files}
        for n, fut in enumerate(as_completed(futures), 1):
            f = futures[fut]
            try:
                for outcome in fut.result():
                    counts[outcome.split(":")[0]] += 1
                    if outcome.startswith("cache conflict"):
                        conflicts.append(f"{f.path}: {outcome}")
            except Exception as e:  # noqa: BLE001
                errors.append((f.path, f"{type(e).__name__}: {e}"))
            if n % 100 == 0:
                log(f"  {n}/{len(plan.files)} files")
    if not errors:
        body = _json_bytes(plan.manifest())
        sha = hashlib.sha256(body).hexdigest()
        if target.stored_sha(MANIFEST_KEY) == sha:
            counts["manifest skipped"] += 1
        else:
            target.put_bytes(MANIFEST_KEY, body, "application/json", sha)
            counts["manifest uploaded"] += 1
    return {"counts": dict(counts), "conflicts": conflicts, "errors": errors}


# -- reporting ------------------------------------------------------------------------------------------------------

def summary(plan: ImportPlan) -> dict[str, Any]:
    """The counts the dry run prints (and the tests check)."""
    files = plan.files
    by_folder: Counter[str] = Counter()
    by_folder_bytes: Counter[str] = Counter()
    for f in files:
        top = f.path.split("/")[0]
        by_folder[top] += 1
        by_folder_bytes[top] += f.bytes
    reasons = Counter(r for _, r in plan.excluded)
    return {
        "files": len(files),
        "bytes": sum(f.bytes for f in files),
        "by_folder": {k: (by_folder[k], by_folder_bytes[k]) for k in sorted(by_folder)},
        "by_type": dict(sorted(Counter(f.content_type for f in files).items())),
        "by_ext": dict(sorted(Counter(PurePosixPath(f.path).suffix.lower() for f in files).items())),
        "with_url": sum(1 for f in files if f.url),
        "without_url": sum(1 for f in files if not f.url),
        "url_from": dict(sorted(Counter(f.url_from for f in files).items())),
        "indexed_by_s3_url": sum(1 for f in files if f.cache_url.startswith("s3://")),
        "with_collected_at": sum(1 for f in files if f.collected_at),
        "cache_entries": sum(1 for f in files if f.writes_cache),
        "excluded": dict(sorted(reasons.items())),
        "excluded_paths": [(p, r) for p, r in plan.excluded if not r.startswith(BULK_REASONS)],
        "warnings": [(f.path, w) for f in files for w in f.warnings],
    }


def plan_coverage(plan: ImportPlan, plan_json: Path) -> list[str] | None:
    """Source ids in data/shl/plan.json whose URL has an archive cache entry (served from the archive)."""
    try:
        sources = json.loads(plan_json.read_text(encoding="utf-8")).get("sources", [])
    except (OSError, json.JSONDecodeError):
        return None
    keys = {f.cache_key for f in plan.files if f.writes_cache}
    return [s["source_id"] for s in sources if s.get("url") and cache.doc_key(s["url"]) in keys]


def _mb(n: int) -> str:
    return f"{n / 1_000_000:,.1f} MB"


def print_dry_run(plan: ImportPlan, *, sample: int, coverage: list[str] | None, out: Callable[[str], None] = print) -> None:
    s = summary(plan)
    out(f"Archive (read only): {plan.root}")
    out(f"DRY RUN, nothing uploaded. Target: s3://{plan.bucket}/{ARCHIVE_PREFIX} and {cache.CACHE_PREFIX}")
    out(f"Included: {s['files']} files, {s['bytes']:,} bytes ({_mb(s['bytes'])})")
    out("By folder:")
    for top, (n, b) in s["by_folder"].items():
        out(f"  {top:<28} {n:>5} files  {_mb(b):>12}")
    out("By content type: " + ", ".join(f"{k} {v}" for k, v in s["by_type"].items()))
    out("By extension:    " + ", ".join(f"{k} {v}" for k, v in s["by_ext"].items()))
    out(f"Original URL known: {s['with_url']} (from {s['url_from']}), unknown: {s['without_url']}")
    out(f"Indexed in the cache under an s3:// URL: {s['indexed_by_s3_url']}")
    out(f"collected_at known: {s['with_collected_at']} of {s['files']}")
    out(f"Cache entries to write: {s['cache_entries']} (files sharing a URL with identical bytes share one entry)")
    out("Excluded inside the included folders, by reason:")
    for reason, n in s["excluded"].items():
        out(f"  {n:>5}  {reason}")
    by_reason: dict[str, list[str]] = defaultdict(list)
    for path, reason in s["excluded_paths"]:
        by_reason[reason].append(path)
    for reason, paths in sorted(by_reason.items()):
        out(f"Excluded, {reason}: {len(paths)}")
        for path in paths[:EXAMPLES]:
            out(f"  {path}")
        if len(paths) > EXAMPLES:
            out(f"  ... and {len(paths) - EXAMPLES} more")
    out("Never walked: every top folder not in the include list (00-Meta except the index and manifest.json, "
        "04-News-Coverage, 06-Macro-Political-Social, 09-My-Analysis, 11-Technical-Explainers, 12-Verification, data)")
    if s["warnings"]:
        out(f"Warnings ({len(s['warnings'])}):")
        for path, w in s["warnings"][:20]:
            out(f"  {path}: {w}")
    if coverage is not None:
        out(f"Plan sources served from the archive cache: {', '.join(coverage) or 'none'}")
    out(f"Sample of {sample} manifest entries:")
    out(json.dumps([manifest_entry(f) for f in pick_sample(plan.files, sample)], indent=2, ensure_ascii=False))
    out(f"Would write {s['files']} archive objects, {MANIFEST_KEY}, and {s['cache_entries']} cache entries "
        f"({2 * s['cache_entries']} objects).")


def pick_sample(files: list[ArchiveFile], n: int) -> list[ArchiveFile]:
    """The first file of each folder, then evenly spaced ones, so the sample shows the variety."""
    picked: list[ArchiveFile] = []
    seen: set[str] = set()
    for f in files:
        top = f.path.split("/")[0]
        if top not in seen:
            seen.add(top)
            picked.append(f)
    if len(picked) < n and files:
        step = max(1, len(files) // n)
        picked += [f for f in files[::step] if f not in picked]
    return picked[:n]


# -- CLI ------------------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    from backend.axelrod.config import get_settings

    ap = argparse.ArgumentParser(description="Import the summer 2026 SHL archive (primary sources only) into the vault.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print counts and a manifest sample, upload nothing")
    mode.add_argument("--upload", action="store_true", help="write archive/shl/, its manifest and the cache index")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="the archive folder (read only)")
    ap.add_argument("--bucket", default=None, help="the vault bucket (default VAULT_BUCKET)")
    ap.add_argument("--target", choices=("s3", "local"), default="s3", help="s3 (default) or DATA_DIR for local mode")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--sample", type=int, default=5)
    ap.add_argument("--include-licensed", action="store_true",
                    help="also include files whose sidecar says 'do not redistribute' (ask Gordon first)")
    ap.add_argument("--replace-cache", action="store_true",
                    help="overwrite a cache entry that holds other bytes (default: keep it and report)")
    ap.add_argument("--plan", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "shl" / "plan.json")
    args = ap.parse_args(argv)

    settings = get_settings()
    bucket = args.bucket or settings.vault_bucket
    if not bucket:
        print("No bucket: set VAULT_BUCKET or pass --bucket (the s3:// URLs in the cache index need it).")
        return 2
    if not args.root.is_dir():
        print(f"Archive folder not found: {args.root}")
        return 2
    plan = build_plan(args.root, bucket, include_licensed=args.include_licensed)
    if args.dry_run:
        print_dry_run(plan, sample=args.sample, coverage=plan_coverage(plan, args.plan))
        return 0

    if args.target == "local":
        target: Any = LocalTarget(settings)
    else:
        import boto3

        target = S3Target(boto3.client("s3", region_name=settings.aws_region), bucket)
    print(f"Uploading {len(plan.files)} files ({_mb(sum(f.bytes for f in plan.files))}) to {target.describe()}")
    result = upload(plan, target, workers=args.workers, replace_cache=args.replace_cache)
    print("Done: " + ", ".join(f"{k} {v}" for k, v in sorted(result["counts"].items())))
    for line in result["conflicts"]:
        print(f"  {line}")
    for path, err in result["errors"]:
        print(f"  ERROR {path}: {err}")
    if result["errors"]:
        print("Manifest not written because some files failed. Rerun: finished objects are skipped.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
