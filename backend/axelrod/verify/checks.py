"""Code checks for every item kind and brief sentence. Each check function returns list[m.CheckResult].

items is RunState.items (id -> item) and sources is RunState.sources_by_id() (id -> Source).
"""
from __future__ import annotations

from ..contract import m


def check_data_item(item: m.DataItem, pages_by_doc: dict[str, list[str]],
                    sources: dict[str, m.Source]) -> list[m.CheckResult]:
    raise NotImplementedError("B-verify")


def check_conclusion(c: m.Conclusion, items: dict[str, m.Item],
                     sources: dict[str, m.Source]) -> list[m.CheckResult]:
    raise NotImplementedError("B-verify")


def check_extrapolation(e: m.Extrapolation, items: dict[str, m.Item], rubric: dict) -> list[m.CheckResult]:
    raise NotImplementedError("B-verify")


def check_chart(g: m.Chart, items: dict[str, m.Item]) -> list[m.CheckResult]:
    raise NotImplementedError("B-verify")


def check_sentence(s: m.SummarySentence, items: dict[str, m.Item], holes: list[m.Hole]) -> list[m.CheckResult]:
    raise NotImplementedError("B-verify")


def check_tier_rules(items: dict[str, m.Item], sources: dict[str, m.Source]) -> list[m.CheckResult]:
    raise NotImplementedError("B-verify")


def status_from(checks: list[m.CheckResult]) -> str:
    """Any fail: fail, any blocked: blocked, else pass."""
    raise NotImplementedError("B-verify")
