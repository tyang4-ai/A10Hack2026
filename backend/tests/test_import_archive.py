"""scripts/import_archive.py on a tiny fake archive under tmp_path (no real archive file, no AWS).

The fake archive copies the real one's layout (folders, `.meta.md` sidecars, the document index, manifest.json)
with made-up names and bytes, so every inclusion and exclusion rule has a case.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from backend.axelrod import cache

REPO = Path(__file__).resolve().parents[2]
BUCKET = "test-vault"
PDF = b"%PDF-1.4\n% fake test pdf\n"


def _load_script():
    """scripts/import_archive.py as a module. Dataclasses look their module up in sys.modules, so register it first."""
    name = "import_archive_script"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "backend" / "scripts" / "import_archive.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ia = _load_script()


# -- the fake archive -----------------------------------------------------------------------------------------------

def _put(root: Path, rel: str, data: bytes | str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return p


def _doc(root: Path, rel: str, data: bytes, url: str, date: str = "2026-08-01", **extra: str) -> None:
    """A primary file plus its sidecar, in the archive's format."""
    _put(root, rel, data)
    lines = ["---", "type: source-document", f'source-url: "{url}"', f"retrieved-date: {date}",
             f"sha256: {extra.pop('sha256', hashlib.sha256(data).hexdigest())}"]
    lines += [f"{k.replace('_', '-')}: {v}" for k, v in extra.items()]
    _put(root, rel + ".meta.md", "\n".join(lines + ["---", "", "Retrieved verbatim."]) + "\n")


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "archive"
    _put(root, "00-Meta/manifest.json", json.dumps(
        {"series": [{"sources": [{"document": "listed", "url": "https://example.org/files/listed.csv"}]}]}))
    _put(root, "00-Meta/_document-index.md", "\n".join([
        "# Document index", "", "## Procurement", "", "`10-Procurement-Evidence/` — **1 documents**", "",
        "### `TED/`", "", "| Document | Retrieved | Size |", "|---|---|---:|",
        "| `listed.csv` | 2026-07-30 | 1 KB |", ""]))
    _put(root, "00-Meta/_collection-state.json", "{}")                      # never opened
    # Included primary sources.
    _doc(root, "01-Annual-Reports/FY2024/FY2024_x_annual-report.pdf", PDF + b"ar2024",
         "HTTPS://Example.COM:443/ir/AR-2024.pdf#page=3")
    _put(root, "01-Annual-Reports/FY2024/FY2024_x_annual-report.md", "# markdown conversion")
    _put(root, "01-Annual-Reports/FY2024/_assets/p1.png", b"\x89PNG")
    _doc(root, "02-Quarterly-Reports/FY2017/keyfigs.pdf", PDF + b"same", "https://example.com/keyfigs.pdf")
    _doc(root, "03-Other-Company-Reports/Press/ir-document.pdf", PDF + b"same", "https://example.com/keyfigs.pdf")
    _doc(root, "05-Regulatory-and-Legal/FDA/a.json", b'{"q": "siemens"}', "https://api.example.gov/device.json")
    _doc(root, "05-Regulatory-and-Legal/FDA/b.json", b'{"q": "varian"}', "https://api.example.gov/device.json")
    _doc(root, "05-Regulatory-and-Legal/FDA/report.pdf", PDF + b"report", "https://example.gov/report.pdf")
    _put(root, "05-Regulatory-and-Legal/FDA/report.pdf.json", '{"pages": 3}')  # metadata about report.pdf
    _doc(root, "05-Regulatory-and-Legal/EU/page.html", b"<!doctype html><p>rule</p>", "https://eur.example.eu/rule")
    _doc(root, "07-Predecessor-Siemens-AG/20-F/f.htm", b"<html>20-F</html>", "https://sec.example.gov/f.htm",
         sha256="0" * 64)                                                    # sidecar hash differs
    _doc(root, "Varian/10-K/v.txt", b"10-K text", "https://sec.example.gov/v.txt")
    _put(root, "10-Procurement-Evidence/TED/listed.csv", "a,b\n1,2\n")        # no sidecar, but in the index
    # Excluded inside included folders.
    _doc(root, "02-Quarterly-Reports/FY2018/slides-lseg.pdf", PDF + b"lseg",
         "n/a — supplied as a file bundle, not retrieved by URL",
         rights="Licensed third-party content. Private research use; do not redistribute.")
    _doc(root, "07-Predecessor-Siemens-AG/20-F/error.pdf", b"<html>404 not found</html>", "https://x.example/e.pdf")
    _put(root, "08-Competitors/P/notes.docx", b"PK docx")
    _put(root, "08-Competitors/P/deck.zip", b"PK zip")
    _put(root, "08-Competitors/P/sheet.xlsx", b"PK xlsx")
    _put(root, "08-Competitors/P/helper.py", "print('never run')")
    _put(root, "08-Competitors/P/_gap-note.md", "note")
    _doc(root, "Varian/data/derived.json", b"{}", "https://example.com/derived.json")
    _doc(root, "Varian/04-News-Coverage/news.html", b"<html>news</html>", "https://news.example.com/a")
    _put(root, "10-Procurement-Evidence/TED/derived.csv", "x,y\n")         # no sidecar, not in the index
    # Never walked at all.
    _doc(root, "09-My-Analysis/model.pdf", PDF + b"gordon", "https://example.com/model.pdf")
    _put(root, "12-Verification/check.json", "{}")
    _put(root, "data/derived.csv", "x\n")
    _put(root, "System/Scripts/collect.py", "raise SystemExit('never run')")
    _doc(root, "04-News-Coverage/article.html", b"<html>news</html>", "https://news.example.com/b")
    return root


INCLUDED = {
    "01-Annual-Reports/FY2024/FY2024_x_annual-report.pdf",
    "02-Quarterly-Reports/FY2017/keyfigs.pdf",
    "03-Other-Company-Reports/Press/ir-document.pdf",
    "05-Regulatory-and-Legal/FDA/a.json",
    "05-Regulatory-and-Legal/FDA/b.json",
    "05-Regulatory-and-Legal/FDA/report.pdf",
    "05-Regulatory-and-Legal/EU/page.html",
    "07-Predecessor-Siemens-AG/20-F/f.htm",
    "Varian/10-K/v.txt",
    "10-Procurement-Evidence/TED/listed.csv",
}


def _by_path(plan) -> dict:
    return {f.path: f for f in plan.files}


# -- what goes in ---------------------------------------------------------------------------------------------------

def test_includes_only_primary_sources(archive: Path) -> None:
    plan = ia.build_plan(archive, BUCKET)
    assert {f.path for f in plan.files} == INCLUDED
    reasons = dict(plan.excluded)
    assert reasons["02-Quarterly-Reports/FY2018/slides-lseg.pdf"].startswith("licensed third-party content")
    assert "not a PDF" in reasons["07-Predecessor-Siemens-AG/20-F/error.pdf"]
    assert reasons["05-Regulatory-and-Legal/FDA/report.pdf.json"] == "json metadata about another file"
    assert reasons["08-Competitors/P/notes.docx"] == "docx"
    assert reasons["08-Competitors/P/deck.zip"] == "zip"
    assert reasons["08-Competitors/P/sheet.xlsx"].startswith("not a primary type")
    assert reasons["08-Competitors/P/helper.py"].startswith("not a primary type")
    assert reasons["01-Annual-Reports/FY2024/FY2024_x_annual-report.md"] == "markdown note"
    assert reasons["01-Annual-Reports/FY2024/_assets/p1.png"] == "asset or index folder"
    assert reasons["Varian/data/derived.json"] == "excluded folder data/"
    assert reasons["Varian/04-News-Coverage/news.html"] == "excluded folder 04-News-Coverage/"
    assert "no provenance" in reasons["10-Procurement-Evidence/TED/derived.csv"]
    walked = {p for p, _ in plan.excluded} | {f.path for f in plan.files}
    assert not [p for p in walked if p.startswith(("09-My-Analysis", "12-Verification", "data/", "System/",
                                                   "04-News-Coverage", "00-Meta"))]


def test_licensed_files_only_on_request(archive: Path) -> None:
    f = _by_path(ia.build_plan(archive, BUCKET, include_licensed=True))["02-Quarterly-Reports/FY2018/slides-lseg.pdf"]
    assert f.url is None and f.url_from == "none"                      # "n/a, supplied as a file bundle"
    assert f.cache_url == f"s3://{BUCKET}/archive/shl/02-Quarterly-Reports/FY2018/slides-lseg.pdf"
    assert f.cache_key == cache.doc_key(f.cache_url)


def test_opens_only_sidecars_meta_files_and_candidate_primaries(archive: Path, monkeypatch) -> None:
    opened: list[str] = []
    real_open = ia._open

    def recording_open(path: Path):
        opened.append(Path(path).relative_to(archive).as_posix())
        return real_open(path)

    monkeypatch.setattr(ia, "_open", recording_open)
    plan = ia.build_plan(archive, BUCKET)
    allowed_meta = {"00-Meta/_document-index.md", "00-Meta/manifest.json"}
    hashed = [p for p in opened if not p.endswith(".meta.md") and p not in allowed_meta]
    assert set(hashed) == INCLUDED | {"07-Predecessor-Siemens-AG/20-F/error.pdf"}   # sniffed, then excluded
    assert all(not p.endswith((".py", ".md")) or p.endswith(".meta.md") or p in allowed_meta for p in opened)
    assert not [p for p in opened if p.startswith(("09-", "12-", "data/", "System/", "04-"))]
    assert "02-Quarterly-Reports/FY2018/slides-lseg.pdf" not in opened  # licensed: only its sidecar is read
    assert len(plan.files) == len(INCLUDED)


# -- provenance and cache keys --------------------------------------------------------------------------------------

def test_url_date_and_content_type(archive: Path) -> None:
    files = _by_path(ia.build_plan(archive, BUCKET))
    ar = files["01-Annual-Reports/FY2024/FY2024_x_annual-report.pdf"]
    assert ar.url == "HTTPS://Example.COM:443/ir/AR-2024.pdf#page=3" and ar.url_from == "sidecar"
    assert ar.collected_at == "2026-08-01T00:00:00.000Z"
    assert ar.content_type == "application/pdf"
    assert ar.sha256 == hashlib.sha256(PDF + b"ar2024").hexdigest() and ar.bytes == len(PDF) + 6
    assert ar.cache_key == cache.doc_key("https://example.com/ir/AR-2024.pdf")   # normalized by cache.py
    listed = files["10-Procurement-Evidence/TED/listed.csv"]
    assert listed.url == "https://example.org/files/listed.csv" and listed.url_from == "manifest"
    assert listed.collected_at == "2026-07-30T00:00:00.000Z"                     # from the document index
    assert listed.content_type == "text/csv"
    assert files["05-Regulatory-and-Legal/EU/page.html"].content_type == "text/html"
    assert files["05-Regulatory-and-Legal/FDA/a.json"].content_type == "application/json"
    assert files["Varian/10-K/v.txt"].content_type == "text/plain"
    assert any("differs from the sidecar" in w for w in files["07-Predecessor-Siemens-AG/20-F/f.htm"].warnings)
    assert not ar.warnings


def test_shared_url_same_bytes_shares_one_entry(archive: Path) -> None:
    files = _by_path(ia.build_plan(archive, BUCKET))
    a, b = files["02-Quarterly-Reports/FY2017/keyfigs.pdf"], files["03-Other-Company-Reports/Press/ir-document.pdf"]
    assert a.cache_url == b.cache_url == "https://example.com/keyfigs.pdf"
    assert a.cache_key == b.cache_key
    assert a.writes_cache and not b.writes_cache


def test_shared_url_different_bytes_falls_back_to_s3_urls(archive: Path) -> None:
    files = _by_path(ia.build_plan(archive, BUCKET))
    a, b = files["05-Regulatory-and-Legal/FDA/a.json"], files["05-Regulatory-and-Legal/FDA/b.json"]
    assert a.url == b.url == "https://api.example.gov/device.json"     # the manifest keeps the original URL
    assert a.cache_url.startswith(f"s3://{BUCKET}/archive/shl/") and a.cache_url != b.cache_url
    assert a.cache_key != b.cache_key and a.writes_cache and b.writes_cache
    assert any("different bytes" in w for w in a.warnings)


def test_cache_meta_is_the_contract_of_cache_py(archive: Path) -> None:
    f = _by_path(ia.build_plan(archive, BUCKET))["01-Annual-Reports/FY2024/FY2024_x_annual-report.pdf"]
    meta = ia.cache_meta(f)
    assert isinstance(meta, cache.CacheMeta)
    assert meta.origin == "archive" and meta.source_path == f.path
    assert meta.normalized_url == "https://example.com/ir/AR-2024.pdf"
    assert cache.doc_key(meta.url) == f.cache_key and meta.sha256 == f.sha256


def test_manifest_entries(archive: Path) -> None:
    man = ia.build_plan(archive, BUCKET).manifest()
    assert man["files"] == len(INCLUDED) and man["label"].startswith("collected before the event")
    entry = next(e for e in man["entries"] if e["path"] == "Varian/10-K/v.txt")
    assert set(entry) >= {"path", "url", "sha256", "bytes", "content_type", "collected_at"}
    assert entry["url"] == "https://sec.example.gov/v.txt"
    assert man["excluded"]["docx"] == 1


# -- upload: local mirror and a fake S3 -----------------------------------------------------------------------------

def test_upload_local_layout_and_idempotent(archive: Path, settings) -> None:
    plan = ia.build_plan(archive, BUCKET)
    target = ia.LocalTarget(settings)
    first = ia.upload(plan, target, log=lambda _: None)
    assert first["errors"] == [] and first["conflicts"] == []
    assert first["counts"] == {"archive uploaded": 10, "cache written": 9, "manifest uploaded": 1}
    f = _by_path(plan)["01-Annual-Reports/FY2024/FY2024_x_annual-report.pdf"]
    assert cache.local_path(settings, cache.archive_key(f.path)).read_bytes() == PDF + b"ar2024"
    assert cache.local_path(settings, cache.body_key(f.cache_key)).read_bytes() == PDF + b"ar2024"
    meta = cache.CacheMeta.model_validate_json(cache.local_path(settings, cache.meta_key(f.cache_key)).read_text("utf-8"))
    assert meta.origin == "archive" and meta.collected_at == "2026-08-01T00:00:00.000Z"
    manifest = json.loads(cache.local_path(settings, cache.ARCHIVE_MANIFEST).read_text("utf-8"))
    assert manifest["files"] == 10
    second = ia.upload(plan, target, log=lambda _: None)
    assert second["counts"] == {"archive skipped": 10, "cache skipped": 9, "manifest skipped": 1}


class FakeS3:
    """The boto3 S3 calls the uploader makes, in memory."""

    def __init__(self, fail_key: str | None = None) -> None:
        self.objects: dict[str, tuple[bytes, dict, str]] = {}
        self.fail_key = fail_key

    @staticmethod
    def _missing(op: str) -> ClientError:
        return ClientError({"Error": {"Code": "404" if op == "HeadObject" else "NoSuchKey"}}, op)

    def head_object(self, Bucket: str, Key: str) -> dict:
        if Key not in self.objects:
            raise self._missing("HeadObject")
        body, meta, ctype = self.objects[Key]
        return {"Metadata": dict(meta), "ContentType": ctype, "ContentLength": len(body)}

    def get_object(self, Bucket: str, Key: str) -> dict:
        if Key not in self.objects:
            raise self._missing("GetObject")
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str, Metadata: dict) -> dict:
        self.objects[Key] = (Body, Metadata, ContentType)
        return {}

    def upload_file(self, Filename: str, Bucket: str, Key: str, ExtraArgs: dict | None = None) -> None:
        if Key == self.fail_key:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        extra = ExtraArgs or {}
        self.objects[Key] = (Path(Filename).read_bytes(), extra.get("Metadata", {}), extra.get("ContentType", ""))

    def copy_object(self, Bucket: str, Key: str, CopySource: dict, MetadataDirective: str, ContentType: str,
                    Metadata: dict) -> dict:
        assert MetadataDirective == "REPLACE" and CopySource["Bucket"] == Bucket
        self.objects[Key] = (self.objects[CopySource["Key"]][0], Metadata, ContentType)
        return {}


def test_upload_s3_idempotent(archive: Path) -> None:
    plan = ia.build_plan(archive, BUCKET)
    s3 = FakeS3()
    target = ia.S3Target(s3, BUCKET)
    first = ia.upload(plan, target, workers=4, log=lambda _: None)
    assert first["counts"] == {"archive uploaded": 10, "cache written": 9, "manifest uploaded": 1}
    f = _by_path(plan)["05-Regulatory-and-Legal/EU/page.html"]
    body, meta, ctype = s3.objects[cache.archive_key(f.path)]
    assert meta == {"sha256": f.sha256} and ctype == "text/html"
    assert s3.objects[cache.body_key(f.cache_key)][0] == body
    assert json.loads(s3.objects[cache.meta_key(f.cache_key)][0])["url"] == "https://eur.example.eu/rule"
    assert len(s3.objects) == 10 + 2 * 9 + 1
    second = ia.upload(plan, target, workers=4, log=lambda _: None)
    assert second["counts"] == {"archive skipped": 10, "cache skipped": 9, "manifest skipped": 1}


def test_existing_cache_entry_with_other_bytes_is_kept(archive: Path) -> None:
    plan = ia.build_plan(archive, BUCKET)
    f = _by_path(plan)["01-Annual-Reports/FY2024/FY2024_x_annual-report.pdf"]
    s3 = FakeS3()
    live = json.dumps({"sha256": "1" * 64, "origin": "cache"}).encode()
    s3.objects[cache.meta_key(f.cache_key)] = (live, {}, "application/json")
    result = ia.upload(plan, ia.S3Target(s3, BUCKET), log=lambda _: None)
    assert result["counts"]["cache conflict"] == 1
    assert result["conflicts"] == [f"{f.path}: cache conflict: kept the existing entry (origin cache)"]
    assert s3.objects[cache.meta_key(f.cache_key)][0] == live
    replaced = ia.upload(plan, ia.S3Target(s3, BUCKET), replace_cache=True, log=lambda _: None)
    assert replaced["counts"]["cache written"] == 1
    assert json.loads(s3.objects[cache.meta_key(f.cache_key)][0])["origin"] == "archive"


def test_failed_file_holds_back_the_manifest(archive: Path) -> None:
    plan = ia.build_plan(archive, BUCKET)
    s3 = FakeS3(fail_key=cache.archive_key("Varian/10-K/v.txt"))
    result = ia.upload(plan, ia.S3Target(s3, BUCKET), log=lambda _: None)
    assert [p for p, _ in result["errors"]] == ["Varian/10-K/v.txt"]
    assert cache.ARCHIVE_MANIFEST not in s3.objects


# -- CLI and small parts --------------------------------------------------------------------------------------------

def test_dry_run_prints_counts_and_writes_nothing(archive: Path, settings, tmp_path: Path, capsys) -> None:
    plan_json = tmp_path / "plan.json"
    plan_json.write_text(json.dumps({"sources": [
        {"source_id": "S-001", "url": "https://example.com/ir/AR-2024.pdf"},
        {"source_id": "S-002", "url": "https://example.com/not-archived.pdf"}]}), encoding="utf-8")
    code = ia.main(["--dry-run", "--root", str(archive), "--bucket", BUCKET, "--plan", str(plan_json)])
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN, nothing uploaded" in out and f"Included: {len(INCLUDED)} files" in out
    assert "Original URL known: 10" in out and "Sample of 5 manifest entries" in out
    assert "Plan sources served from the archive cache: S-001\n" in out
    assert "Excluded, licensed third-party content" in out
    assert not any(Path(settings.data_dir).iterdir())                 # nothing written


def test_cli_needs_a_bucket(archive: Path, capsys) -> None:
    assert ia.main(["--dry-run", "--root", str(archive)]) == 2       # conftest leaves VAULT_BUCKET unset
    assert "VAULT_BUCKET" in capsys.readouterr().out


def test_sniff() -> None:
    assert ia.sniff(PDF, ".pdf") == ("application/pdf", None)
    assert ia.sniff(b"<html>gone</html>", ".pdf")[1].startswith("named .pdf")
    assert ia.sniff(b"", ".txt") == (None, "empty file")
    assert ia.sniff(b"\xef\xbb\xbf [1]", ".json") == ("application/json", None)
    assert ia.sniff(b"<p>", ".json")[1] == "named .json but not JSON"
    assert ia.sniff(b"<?xml version='1.0'?>", ".xml") == ("application/xml", None)


def test_parse_frontmatter() -> None:
    text = '---\r\ntype: source-document\r\nsource-url: "https://a.example/x.pdf"\r\nrights: \'ok\'\r\n---\r\nbody: no\r\n'
    assert ia.parse_frontmatter(text) == {"type": "source-document", "source-url": "https://a.example/x.pdf",
                                          "rights": "ok"}
    assert ia.parse_frontmatter("no front matter") == {}
