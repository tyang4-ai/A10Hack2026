"""The vault: raw files, page text, reports and transcripts per run (CONTRACT.md section 10 paths).
Local mode writes under DATA_DIR, aws mode writes to the S3 bucket.

Every file lives at `runs/<run_id>/<company-slug>/<relpath>`, and that full string is the vault path the
API shows. Methods take either the relpath ("00-Meta/_plan.json") or the full vault path, and reject
anything that would escape the run's folder.
"""
from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import Any, get_args

from ..config import BACKEND_DIR, Settings
from ..contract import m

FOLDERS: list[str] = list(get_args(m.VaultFolder))
TEXT_LIMIT = 1_000_000                       # VaultFile.text carries at most this many characters
PRESIGN_SECONDS = 900

TEXT_TYPES = {".json": "application/json", ".jsonl": "application/x-ndjson", ".md": "text/markdown",
              ".txt": "text/plain", ".html": "text/html", ".htm": "text/html", ".csv": "text/csv",
              ".xml": "application/xml"}
BINARY_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".zip": "application/zip"}

_LEGAL_FORMS = {"ag", "se", "inc", "corp", "corporation", "plc", "ltd", "llc", "nv", "sa", "co", "gmbh", "kgaa",
                "company", "holding", "holdings", "group"}

_roots: dict[str, str] = {}                  # run_id -> "runs/<run_id>/<slug>"
_replays: dict[str, Path] = {}               # run_id -> local folder with a replay's vault files
_registry_lock = threading.Lock()


def company_slug(company: str) -> str:
    """'Siemens Healthineers AG' -> 'siemens-healthineers'. Drops parentheses and trailing legal forms."""
    text = re.sub(r"\([^)]*\)", " ", company.lower())
    words = re.findall(r"[a-z0-9]+", text)
    while len(words) > 1 and words[-1] in _LEGAL_FORMS:
        words.pop()
    return "-".join(words) or "company"


def register_run(run_id: str, company: str) -> str:
    """Remember the run's vault root. The Store calls this on create and on load."""
    root = f"runs/{run_id}/{company_slug(company)}"
    with _registry_lock:
        _roots[run_id] = root
    return root


def unregister_run(run_id: str) -> None:
    """Forget a deleted run's vault root, so nothing serves or rediscovers it from memory."""
    with _registry_lock:
        _roots.pop(run_id, None)


def register_replay(run_id: str, folder: Path) -> None:
    """A replay's vault files are read from its folder under backend/data/replay (read-only)."""
    with _registry_lock:
        _replays[run_id] = folder


def data_root(settings: Settings) -> Path:
    """DATA_DIR, resolved against the repo root when it is relative."""
    p = Path(settings.data_dir)
    return p if p.is_absolute() else BACKEND_DIR.parent / p


def content_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    return TEXT_TYPES.get(ext) or BINARY_TYPES.get(ext) or "application/octet-stream"


def is_text(path: str) -> bool:
    return Path(path).suffix.lower() in TEXT_TYPES


class Vault:
    def __init__(self, settings: Settings, *, s3: Any = None) -> None:
        self.settings = settings
        self._s3 = s3

    # -- paths ----------------------------------------------------------------------------

    @property
    def aws(self) -> bool:
        return self.settings.store == "aws"

    def s3(self) -> Any:
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3", region_name=self.settings.aws_region)
        return self._s3

    @property
    def bucket(self) -> str:
        if not self.settings.vault_bucket:
            raise RuntimeError("VAULT_BUCKET is not set (STORE=aws needs it)")
        return self.settings.vault_bucket

    def root(self, run_id: str) -> str:
        """runs/<run_id>/<slug>. Registered runs answer from memory; others are discovered once."""
        with _registry_lock:
            if run_id in _roots:
                return _roots[run_id]
        root = self._discover_root(run_id) or f"runs/{run_id}/company"
        with _registry_lock:
            _roots.setdefault(run_id, root)
            return _roots[run_id]

    def _discover_root(self, run_id: str) -> str | None:
        if not re.fullmatch(r"run_[A-Za-z0-9_]+", run_id):
            return None
        if self.aws:
            resp = self.s3().list_objects_v2(Bucket=self.bucket, Prefix=f"runs/{run_id}/", Delimiter="/")
            prefixes = [p["Prefix"].rstrip("/") for p in resp.get("CommonPrefixes", [])]
            return prefixes[0] if prefixes else None
        base = data_root(self.settings) / "runs" / run_id
        dirs = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
        return f"runs/{run_id}/{dirs[0]}" if dirs else None

    def rel(self, run_id: str, path: str) -> str:
        """The relpath inside the run's folder. Raises ValueError for paths outside it or with '..'."""
        root = self.root(run_id)
        p = path.replace("\\", "/").lstrip("/")
        if p == root or p.startswith(root + "/"):
            p = p[len(root):].lstrip("/")
        elif p.startswith("runs/"):
            raise ValueError(f"{path} is not inside {root}")
        parts = [x for x in p.split("/") if x]
        if not parts or any(x in (".", "..") or ":" in x for x in parts):
            raise ValueError(f"bad vault path {path!r}")
        return "/".join(parts)

    def vault_path(self, run_id: str, relpath: str) -> str:
        return f"{self.root(run_id)}/{self.rel(run_id, relpath)}"

    def _local(self, run_id: str, rel: str) -> Path:
        return data_root(self.settings) / self.root(run_id) / rel

    # -- writes ---------------------------------------------------------------------------

    def put_bytes(self, run_id: str, relpath: str, data: bytes) -> str:
        """Store data at relpath inside the run's vault. Returns the vault path as the API shows it.
        Sync: from async code in aws mode, prefer aput_bytes."""
        rel = self.rel(run_id, relpath)
        if self.aws:
            self.s3().put_object(Bucket=self.bucket, Key=f"{self.root(run_id)}/{rel}", Body=data,
                                 ContentType=content_type(rel))
        else:
            target = self._local(run_id, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
        return f"{self.root(run_id)}/{rel}"

    def put_text(self, run_id: str, relpath: str, text: str) -> str:
        """UTF-8 text version of put_bytes. Returns the vault path."""
        return self.put_bytes(run_id, relpath, text.encode("utf-8"))

    async def aput_bytes(self, run_id: str, relpath: str, data: bytes) -> str:
        """put_bytes off the event loop when it goes to S3."""
        if self.aws:
            return await asyncio.to_thread(self.put_bytes, run_id, relpath, data)
        return self.put_bytes(run_id, relpath, data)

    async def aput_text(self, run_id: str, relpath: str, text: str) -> str:
        return await self.aput_bytes(run_id, relpath, text.encode("utf-8"))

    # -- reads ----------------------------------------------------------------------------

    def get_bytes(self, run_id: str, relpath: str) -> bytes:
        """Raises KeyError when the file does not exist."""
        rel = self.rel(run_id, relpath)
        data = self._read_primary(run_id, rel)
        if data is None:
            data = self._read_replay(run_id, rel)
        if data is None:
            raise KeyError(f"no vault file {self.root(run_id)}/{rel}")
        return data

    def get_text(self, run_id: str, relpath: str) -> str:
        return self.get_bytes(run_id, relpath).decode("utf-8", errors="replace")

    def _read_primary(self, run_id: str, rel: str) -> bytes | None:
        if self.aws:
            try:
                return self.s3().get_object(Bucket=self.bucket, Key=f"{self.root(run_id)}/{rel}")["Body"].read()
            except Exception as e:  # botocore NoSuchKey, or a fake client's KeyError
                if _is_missing(e):
                    return None
                raise
        p = self._local(run_id, rel)
        return p.read_bytes() if p.is_file() else None

    def _read_replay(self, run_id: str, rel: str) -> bytes | None:
        folder = _replays.get(run_id)
        if folder is None:
            return None
        p = folder / rel
        return p.read_bytes() if p.is_file() else None

    def list_files(self, run_id: str) -> dict[str, int]:
        """relpath -> size for every file in the run's vault (store plus replay folder)."""
        files: dict[str, int] = {}
        folder = _replays.get(run_id)
        if folder is not None and folder.is_dir():
            files.update(_walk(folder))
        if self.aws:
            prefix = self.root(run_id) + "/"
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            while True:
                resp = self.s3().list_objects_v2(**kwargs)
                for obj in resp.get("Contents", []):
                    files[obj["Key"][len(prefix):]] = int(obj["Size"])
                if not resp.get("IsTruncated"):
                    break
                kwargs["ContinuationToken"] = resp["NextContinuationToken"]
        else:
            base = data_root(self.settings) / self.root(run_id)
            if base.is_dir():
                files.update(_walk(base))
        return files

    # -- API views ------------------------------------------------------------------------

    def tree(self, run_id: str) -> m.VaultNode:
        """The 13 vault folders (always shown, even empty) with every stored file under them."""
        root = self.root(run_id)
        top = {"name": root.rsplit("/", 1)[-1], "path": root, "type": "dir", "size": None, "children": []}
        index: dict[str, dict] = {}
        for f in FOLDERS:
            node = {"name": f, "path": f"{root}/{f}", "type": "dir", "size": None, "children": []}
            top["children"].append(node)
            index[node["path"]] = node
        for rel, size in sorted(self.list_files(run_id).items()):
            parts = rel.split("/")
            cur = top
            for depth, part in enumerate(parts):
                path = f"{root}/" + "/".join(parts[:depth + 1])
                node = index.get(path)
                if node is None:
                    last = depth == len(parts) - 1
                    node = {"name": part, "path": path, "type": "file" if last else "dir",
                            "size": size if last else None, "children": None if last else []}
                    cur["children"].append(node)
                    index[path] = node
                cur = node
        return m.VaultNode.model_validate(top)

    def file(self, run_id: str, path: str) -> m.VaultFile:
        """Text up to 1 MB inline, binaries as a presigned URL in aws mode. KeyError or ValueError: no such file."""
        rel = self.rel(run_id, path)
        data = self.get_bytes(run_id, rel)
        full = f"{self.root(run_id)}/{rel}"
        url = None
        if self.aws and (not is_text(rel) or len(data) > TEXT_LIMIT):
            url = self.s3().generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": full},
                                                   ExpiresIn=PRESIGN_SECONDS)
        if not is_text(rel):
            return m.VaultFile(path=full, content_type=content_type(rel), size=len(data), text=None,
                               truncated=False, download_url=url)
        text = data.decode("utf-8", errors="replace")
        return m.VaultFile(path=full, content_type=content_type(rel), size=len(data), text=text[:TEXT_LIMIT],
                           truncated=len(text) > TEXT_LIMIT, download_url=url)


def _walk(base: Path) -> dict[str, int]:
    return {p.relative_to(base).as_posix(): p.stat().st_size
            for p in base.rglob("*") if p.is_file() and not p.name.endswith(".tmp")}


def _is_missing(e: Exception) -> bool:
    if isinstance(e, KeyError):
        return True
    code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
    return code in ("NoSuchKey", "404", "NotFound")
