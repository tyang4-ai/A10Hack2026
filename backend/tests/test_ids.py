"""Id and timestamp formats from PLANS.md "Shared interfaces" and CONTRACT.md section 1."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from backend.axelrod.ids import IdAllocator, new_run_id, now_iso


def test_now_iso_format():
    s = now_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", s), s
    parsed = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    assert abs(datetime.now(timezone.utc) - parsed) < timedelta(seconds=5)


def test_new_run_id_format():
    before = datetime.now(timezone.utc).strftime("%Y%m%d")
    rid = new_run_id()
    after = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert re.fullmatch(r"run_\d{8}_[a-z0-9]{6}", rid), rid
    assert rid[4:12] in (before, after)


def test_new_run_id_unique():
    ids = {new_run_id() for _ in range(200)}
    assert len(ids) == 200


def test_widths():
    a = IdAllocator()
    assert a.next("N") == "N-01"
    assert a.next("HOLE") == "HOLE-01"
    assert a.next("REQ") == "REQ-0001"
    assert a.next("D") == "D-001"
    assert a.next("CM") == "CM-001"
    for prefix in ("S", "DOC", "C", "E", "G", "H"):
        assert a.next(prefix) == f"{prefix}-001"
    assert a.next("extractor") == "extractor-01"
    assert a.next("fetcher") == "fetcher-01"


def test_counts_per_prefix():
    a = IdAllocator()
    assert [a.next("D") for _ in range(3)] == ["D-001", "D-002", "D-003"]
    assert a.next("C") == "C-001"
    assert a.next("D-") == "D-004"  # a trailing dash means the same prefix
    assert a.counters == {"D": 4, "C": 1}


def test_wider_than_padding():
    a = IdAllocator(counters={"N": 99, "D": 999})
    assert a.next("N") == "N-100"
    assert a.next("D") == "D-1000"


def test_allocators_do_not_share_counters():
    a, b = IdAllocator(), IdAllocator()
    a.next("D")
    assert b.next("D") == "D-001"


def test_survives_a_restart():
    a = IdAllocator()
    a.next("D")
    a.next("D")
    a.next("REQ")
    b = IdAllocator.model_validate_json(a.model_dump_json())
    assert b.next("D") == "D-003"
    assert b.next("REQ") == "REQ-0002"
