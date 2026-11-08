"""Summarizer agent (large model): the 3-page brief, every sentence cited to report items. Owner: B-brief.

This module also holds the brief's two helper agents, because their prompts read the same item views:
the faithfulness judge (role verifier, small model, three per sentence, isolated) and the sentence
repairer (role repairer, small model), which may only weaken a sentence toward its evidence.

Output models carry no new ids. Sentences cite ids the prompt showed them, and code checks every one
(citations_resolve) and assigns every H- id. The fakes below answer when FAKE_MODELS=1: they build an
offline brief from the prompt's own JSON, so the whole pipeline runs without Bedrock.

Holes: the brief's gap section is a short summary that code writes and counts (stages/summarize.py), not
the register. A judge checking a counted gap sentence gets a code-made tally of the holes it cites
(hole_tally), so it never has to count a long list itself.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from ..contract import RUBRIC, m
from .base import register_fake, tool_name

PASSING = ("pass", "fixed")
_KIND_ORDER = {"extrapolation": 0, "conclusion": 1, "chart": 2, "data": 3}
_CATEGORY_ORDER = [c["id"] for c in RUBRIC["categories"]]
_CATEGORY_LABEL = {c["id"]: c["label"] for c in RUBRIC["categories"]}
EVIDENCE_LIMIT = 40          # items a judge or repairer sees at most (cited ones first, then their inputs)
KIND_WORDS = {"data": ("data item", "data items"), "conclusion": ("conclusion", "conclusions"),
              "chart": ("chart", "charts"), "extrapolation": ("score", "scores")}
_SOURCE_REF = re.compile(r"\bS-\d{3}\b")
_AGENT_HOLE = re.compile(r"^[a-z]+-\d+ failed\b")    # finalize._agent_reason: "extractor-07 failed: ..."


# ---------------------------------------------------------------------------
# Output models (one forced tool each)
# ---------------------------------------------------------------------------


class BriefSentenceOut(BaseModel):
    text: str = Field(description="One sentence. Never write an id in it.")
    mode: Literal["copied", "derived"] = Field(
        description="copied: the text is exactly one item's text and derived_from is that one id. "
                    "derived: your own summary that says no more than the cited items.")
    derived_from: list[str] = Field(
        description="The ids this sentence rests on: D-, C-, E- or G- item ids from the input, "
                    "'rollup' for scorecard numbers, HOLE- ids for gaps.")


class BriefBlockOut(BaseModel):
    type: Literal["paragraph", "bullets", "chart", "scorecard"]
    sentences: list[BriefSentenceOut] = Field(default_factory=list, description="paragraph and bullets only")
    chart_id: str | None = Field(default=None, description="chart only: a G- id from the input")
    caption: BriefSentenceOut | None = Field(default=None, description="chart only: one sentence that cites the chart")


class BriefSectionOut(BaseModel):
    heading: str
    blocks: list[BriefBlockOut]


# The nesting stays (section, block, sentence). In R0 Sonnet 5 filled it correctly on both tries and only
# wrapped the whole input in a JSON string ({"sections": "{\"sections\": [...]}"}), which AgentRunner now
# parses back (base.coerce_json_strings). The docstring below is the tool description the model reads.
class BriefDraft(BaseModel):
    """The whole brief as sections of paragraph, bullets, chart and scorecard blocks. Every sentence cites
    the ids it rests on. sections is a JSON array of section objects, never a string holding JSON."""
    TOOL_NAME: ClassVar[str] = "brief_draft"
    sections: list[BriefSectionOut] = Field(
        description="The sections in reading order, as a JSON array of objects (not a string).")


class FaithfulnessVerdict(BaseModel):
    """Your verdict on one brief sentence: supported only if the evidence states every part of it."""
    TOOL_NAME: ClassVar[str] = "brief_faithfulness"
    supported: bool
    unsupported_spans: list[str] = Field(
        default_factory=list, description="Exact words copied from the sentence that the evidence does not support.")
    reason: str = Field(default="", description="One short sentence.")


class BriefRewrite(BaseModel):
    """The repaired sentence, weaker and closer to its evidence, or delete when nothing supported is left."""
    TOOL_NAME: ClassVar[str] = "brief_rewrite"
    action: Literal["rewrite", "delete"]
    text: str = Field(default="", description="The repaired sentence when action is rewrite.")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SUMMARIZER_SYSTEM = """You write a three-page screening brief for an equity analyst from a verified agent report.

Rules:
1. Use only the items, the rollup and the holes you are given. Never add a fact, a cause, a forecast, a comparison or an opinion that the cited items do not state.
2. Every sentence lists in derived_from the ids it rests on: item ids (D-, C-, E-, G-), "rollup" for scorecard numbers (weighted average, the two percentages, criteria counts), and HOLE- ids for gaps. Cite only ids that appear in the input. Never write an id in the sentence text.
3. mode "copied": the text is exactly one item's "text", character for character, and derived_from is that one id. Copy when an item already says it well.
4. mode "derived": a summary in your own words that says no more than its cited items. Every number in it must be a cited item's value as shown (you may round it half up to fewer decimals), a cited computation's display, a cited chart's point, a cited score (-1, 0, +1) or its rule threshold, or a rollup number. Other numbers inside an item's text do not count. Never compute a number yourself: no sums, differences, ratios or growth rates. Years and fiscal-year labels are fine. Spell out small counts in words ("three years", "two sources") unless the count is a rollup number. Everything else (amounts, percentages, ratios, scores, rollup numbers) is written in digits, never in words, so code can trace it.
5. Structure, in this order:
   - "Bottom line": a short paragraph with the weighted average and the coverage (cite "rollup") and the main reasons (cite the E and C items).
   - One section per category in categories_in_scope, in that order, headed by its label, as a paragraph or bullets built from its conclusions, scores and data. Every one of these categories gets at least one sentence. A category whose criteria are not scored still gets a sentence from its verified conclusions or data, or a copy of its score item's text.
   - At most two chart blocks, each with a chart_id from the input and a caption sentence that cites that chart (copy its text, or describe it using only its points).
   - A section "Scorecard" with one scorecard block and no sentences. Code renders the rollup table.
   - Last, "What we could not determine": one bullets block holding the sentences in gap_summary, copied exactly as given, each with mode "derived" and its derived_from. Code wrote and counted them from the hole register, which the analyst sees in full next to the brief. Add nothing to this section, and write no other sentence that cites only HOLE ids. Elsewhere you may cite a HOLE id next to the item the sentence is about.
6. Length: at most {word_budget} words in all sentences together, not counting the gap_summary sentences, so the brief fits three printed pages with the scorecard and the charts. Spend the words on the categories first.
7. Style: plain neutral words, short sentences, no hype, no advice to buy or sell. Write currency and scale the way the items show them, for example "€1,234 million"."""

JUDGE_SYSTEM = """You check one sentence from an analyst brief against the evidence it cites. You see only that sentence and its evidence.

Answer supported=true only if every part of the sentence is directly stated in the evidence or follows from it by plain reading: the numbers, the direction of any change, the period, the subject, and any cause or comparison. Rounding a number half up is fine. A sentence that says a claim failed verification or a criterion is not scored is supported when the evidence shows that. Counts written in words ("five data items") are checked against the hole_count entry, which code counted over the cited holes.
Otherwise answer supported=false and list in unsupported_spans the exact words of the sentence that go beyond the evidence, copied character for character.
Use no outside knowledge. Missing support counts as unsupported. reason is one short sentence."""

REWRITE_SYSTEM = """You repair one sentence of an analyst brief that failed verification. You may only weaken it toward its evidence: remove or soften the words the checks flagged, put back a number exactly as the evidence shows it, drop a cause, forecast or comparison the evidence does not state.
Never add a claim, a number or a citation. Keep one sentence in plain words, and never write an id in it.
If nothing supported is left, answer action "delete"."""


def summarizer_system(word_budget: int) -> str:
    return SUMMARIZER_SYSTEM.replace("{word_budget}", str(word_budget))


def _block(tag: str, obj: Any) -> str:
    return f"<{tag}>\n{json.dumps(obj, ensure_ascii=False, indent=1, default=str)}\n</{tag}>"


def summarizer_user(payload: dict) -> str:
    who = payload["company"] + (f" ({payload['ticker']})" if payload.get("ticker") else "")
    return (f"Write the brief for {who}, as of {payload['as_of']}. Call {tool_name(BriefDraft)} once, with "
            "sections as a JSON array of objects, not a string.\n\n" + _block("report", payload))


def judge_user(sentence: str, evidence: list[dict]) -> str:
    return (f"Check this sentence against its evidence. Call {tool_name(FaithfulnessVerdict)} once.\n\n"
            f"<sentence>\n{sentence}\n</sentence>\n\n" + _block("evidence", evidence))


def rewrite_user(sentence: str, failed: list[dict], evidence: list[dict]) -> str:
    task = {"sentence": sentence, "failed_checks": failed, "evidence": evidence}
    return f"Repair this sentence. Call {tool_name(BriefRewrite)} once.\n\n" + _block("task", task)


# ---------------------------------------------------------------------------
# Views of the report: what the models see
# ---------------------------------------------------------------------------


def criterion_label(criterion: str | None) -> str | None:
    if not criterion:
        return None
    return (RUBRIC["criteria"].get(criterion) or {}).get("label") or criterion


def item_text(it: m.Item) -> str:
    """The text a copied sentence must equal (the same field copy_exact reads)."""
    if isinstance(it, m.DataItem):
        return it.statement
    return it.title if isinstance(it, m.Chart) else it.text


def item_view(it: m.Item, *, with_status: bool = False) -> dict:
    """One item as the models see it: its text, the numbers it vouches for, and its inputs."""
    if isinstance(it, m.DataItem):
        v = it.value
        out = {"id": it.id, "kind": "data", "category": it.category, "text": it.statement,
               "value": None if v is None else {"as_reported": v.as_reported, "unit": v.unit, "scale": v.scale,
                                                "period": v.period},
               "source_tier": it.source_tier}
    elif isinstance(it, m.Conclusion):
        out = {"id": it.id, "kind": "conclusion", "category": it.category, "criteria": list(it.criteria),
               "text": it.text, "data_ids": list(it.data_ids),
               "computations": [{"name": c.name, "display": c.display} for c in it.computations]}
    elif isinstance(it, m.Extrapolation):
        out = {"id": it.id, "kind": "extrapolation", "category": it.category, "criterion": it.criterion,
               "criterion_label": criterion_label(it.criterion), "score": it.score, "na_reason": it.na_reason,
               "confidence": it.confidence, "text": it.text, "conclusion_ids": list(it.conclusion_ids),
               "rule": {"type": it.rule.type, "plus_one": it.rule.plus_one, "minus_one": it.rule.minus_one}}
    else:
        out = {"id": it.id, "kind": "chart", "category": it.category, "text": it.title, "chart_type": it.chart_type,
               "unit": it.unit, "scale": it.scale,
               "points": [{"series": s.name, "x": p.x, "y": p.y} for s in it.series for p in s.points]}
    if with_status:
        out["status"] = it.verification.status
    return out


def rollup_view(r: m.Rollup | None) -> dict | None:
    if r is None:
        return None
    return {"weighted_average": r.weighted_average, "normalized_pct": r.normalized_pct,
            "percent_of_max_pct": r.percent_of_max_pct, "scored": r.scored, "in_scope": r.in_scope,
            "na_criteria": [criterion_label(k) for k in r.na_criteria],
            "categories": [{"category": _CATEGORY_LABEL.get(c.category, c.category), "weight": c.weight,
                            "score": c.score, "scored": c.scored, "in_scope": c.in_scope} for c in r.categories],
            "note": r.note}


def plain_label(criterion: str | None) -> str:
    """'ROTIC level (10y)' -> 'ROTIC level', so a gap sentence naming a criterion carries no stray number."""
    return re.sub(r"\s*\([^)]*\)", "", criterion_label(criterion) or "").strip() or "A criterion"


def hole_kind(h: m.Hole) -> str:
    """How the brief groups a hole: criterion, item, agent, source or other (reasons come from finalize)."""
    if h.criterion:
        return "criterion"
    if h.item_id:
        return "item"
    if _AGENT_HOLE.match(h.reason):
        return "agent"
    return "source" if _SOURCE_REF.search(h.reason) else "other"


def item_criterion(it: m.Item | None) -> str | None:
    """The first criterion an item serves (charts serve none)."""
    if isinstance(it, m.Extrapolation):
        return it.criterion
    crits = getattr(it, "criteria", None) or []
    return crits[0] if crits else None


def left_out(holes: list[m.Hole], items: dict[str, m.Item]) -> tuple[Counter, Counter, Counter]:
    """Over the item holes: (failed by kind, not verified by kind, by criterion label or None)."""
    failed, unverified, by_crit = Counter(), Counter(), Counter()
    for h in holes:
        if hole_kind(h) != "item":
            continue
        it = items.get(h.item_id)
        kind = it.kind if it is not None else "data"
        (failed if it is None or it.verification.status == "fail" else unverified)[kind] += 1
        crit = item_criterion(it)
        by_crit[plain_label(crit) if crit else None] += 1
    return failed, unverified, by_crit


def hole_tally(holes: list[m.Hole], items: dict[str, m.Item]) -> dict:
    """Counts over the holes a sentence cites, made by code: the judge checks counted words against these."""
    out: dict[str, Any] = {"id": "hole_count", "kind": "count",
                           "note": "Counted by code over the holes this sentence cites.", "holes": len(holes)}
    crit = [plain_label(h.criterion) for h in holes if h.criterion]
    if crit:
        out["criteria_not_scored"] = crit
    failed, unverified, by_crit = left_out(holes, items)
    if failed:
        out["items_that_failed_verification"] = {KIND_WORDS[k][1]: n for k, n in failed.items()}
    if unverified:
        out["items_that_could_not_be_verified"] = {KIND_WORDS[k][1]: n for k, n in unverified.items()}
    if by_crit:
        out["items_by_criterion"] = {(c or "no criterion"): n for c, n in by_crit.most_common()}
    kinds = Counter(hole_kind(h) for h in holes)
    if kinds["source"]:
        out["sources_not_used"] = kinds["source"]
    if kinds["agent"]:
        out["extractor_agents_failed"] = kinds["agent"]
    return out


def hole_view(h: m.Hole, items: dict[str, m.Item] | None = None) -> dict:
    """One hole. With items, a judge also sees the kind, status and criterion of the item it is about."""
    out = {"id": h.hole_id, "kind": "hole", "criterion": h.criterion, "criterion_label": criterion_label(h.criterion),
           "item_id": h.item_id, "reason": h.reason}
    it = (items or {}).get(h.item_id) if h.item_id else None
    if it is not None:
        crit = item_criterion(it)
        out.update(item_kind=it.kind, item_status=it.verification.status,
                   item_criterion=plain_label(crit) if crit else None)
    return out


def scope_criteria(depth: str) -> list[str]:
    """In-scope criteria in rubric order (the same rule as finalize.in_scope)."""
    d = RUBRIC["depths"].get(depth)
    if d == "all" or d is None:
        return [k for c in RUBRIC["categories"] for k in c["criteria"]]
    return list(d)


def categories_in_scope(state) -> list[dict]:
    """In-scope categories that have verified items, in rubric order: each one must get a brief sentence."""
    scope = scope_criteria(state.run.depth)
    have = {i.category for i in state.items.values() if i.verification.status in PASSING}
    out = []
    for c in RUBRIC["categories"]:
        crits = [k for k in c["criteria"] if k in scope]
        if crits and c["id"] in have:
            out.append({"category": c["label"], "criteria": [criterion_label(k) for k in crits]})
    return out


def _sort_key(it: m.Item) -> tuple:
    cat = _CATEGORY_ORDER.index(it.category) if it.category in _CATEGORY_ORDER else len(_CATEGORY_ORDER)
    return (_KIND_ORDER.get(it.kind, 9), cat, it.id)


def report_payload(state, *, word_budget: int, gap_summary: list[dict] | None = None) -> dict:
    """Everything the summarizer may use: verified items only (pass or fixed), the rollup, the holes, the
    categories it must cover and the gap sentences code wrote."""
    run = state.run
    items = sorted((i for i in state.items.values() if i.verification.status in PASSING), key=_sort_key)
    return {"company": run.company, "ticker": run.ticker, "depth": run.depth,
            "as_of": state.as_of or run.created_at[:10], "fiscal_note": state.fiscal_note,
            "word_budget": word_budget, "categories_in_scope": categories_in_scope(state),
            "items": [item_view(i) for i in items], "rollup": rollup_view(state.rollup),
            "holes": [hole_view(h) for h in state.holes], "gap_summary": list(gap_summary or [])}


def _inputs(it: m.Item) -> list[str]:
    if isinstance(it, m.Conclusion):
        return list(it.data_ids)
    if isinstance(it, m.Extrapolation):
        return list(it.conclusion_ids)
    if isinstance(it, m.Chart):
        return list(it.refs)
    return []


def evidence_for(refs: list[str], items: dict[str, m.Item], holes: list[m.Hole], rollup: m.Rollup | None,
                 *, limit: int = EVIDENCE_LIMIT) -> list[dict]:
    """The cited refs first, then their inputs (E to C to D, G to refs), each once. Nothing else, except a
    code-made count over the cited holes when there are two or more (it comes first, so no limit cuts it)."""
    by_hole = {h.hole_id: h for h in holes}
    cited = [by_hole[b] for b in dict.fromkeys(r.split(":")[0] for r in refs) if b in by_hole]
    queue, seen = list(refs), set()
    out: list[dict] = [hole_tally(cited, items)] if len(cited) > 1 else []
    while queue and len(out) < limit:
        base = queue.pop(0).split(":")[0]
        if base in seen:
            continue
        seen.add(base)
        if base == "rollup":
            if rollup is not None:
                out.append({"id": "rollup", "kind": "rollup", **rollup_view(rollup)})
        elif base in by_hole:
            out.append(hole_view(by_hole[base], items))
        elif base in items:
            out.append(item_view(items[base], with_status=True))
            queue.extend(_inputs(items[base]))
    return out


# ---------------------------------------------------------------------------
# Fakes (FAKE_MODELS=1): offline answers built from the prompt's own JSON
# ---------------------------------------------------------------------------


def _tagged(text: str, tag: str) -> Any:
    start, end = text.find(f"<{tag}>"), text.rfind(f"</{tag}>")
    if start < 0 or end < start:
        return None
    return json.loads(text[start + len(tag) + 2:end])


def _s(text: str, refs: list[str], mode: str = "derived") -> dict:
    return {"text": text, "mode": mode, "derived_from": refs}


FAKE_MAX_WORDS = 60          # the fake copies short items only, as a summarizer keeps sentences short


def _short(item: dict) -> bool:
    return len(item["text"].split()) <= FAKE_MAX_WORDS


def fake_brief(system: str, user: str, schema: dict) -> dict:
    """A deterministic brief: the rollup line, copied conclusions and scores, charts, scorecard, and the
    gap summary copied from the prompt, as the prompt asks."""
    p = _tagged(user, "report") or {}
    items = p.get("items", [])
    sections: list[dict] = []
    r = p.get("rollup")
    bottom = []
    if r and r.get("weighted_average") is not None:
        bottom.append(_s(f"{p.get('company', 'The company')} scores {r['weighted_average']:+.2f} on the BOFFIN "
                         f"{p.get('depth', 'screen')}, with {r['scored']} of {r['in_scope']} criteria scored.",
                         ["rollup"]))
    scored = [i for i in items if i["kind"] == "extrapolation" and i.get("score") is not None and _short(i)]
    lead = scored[0] if scored else None
    if lead is not None:
        bottom.append(_s(lead["text"], [lead["id"]], "copied"))
    if bottom:
        sections.append({"heading": "Bottom line", "blocks": [{"type": "paragraph", "sentences": bottom}]})
    charts_left = 2
    for cat in _CATEGORY_ORDER:
        mine = [i for i in items if i["category"] == cat]
        concl = [_s(i["text"], [i["id"]], "copied") for i in mine if i["kind"] == "conclusion" and _short(i)][:3]
        scores = [_s(i["text"], [i["id"]], "copied") for i in scored if i["category"] == cat and i is not lead]
        blocks: list[dict] = []
        if concl:
            blocks.append({"type": "paragraph", "sentences": concl})
        if scores:
            blocks.append({"type": "bullets", "sentences": scores})
        for g in (i for i in mine if i["kind"] == "chart"):
            if charts_left:
                charts_left -= 1
                blocks.append({"type": "chart", "chart_id": g["id"], "caption": _s(g["text"], [g["id"]], "copied")})
        if not blocks and mine:      # every category gets a sentence: its shortest score, conclusion or data text
            pick = min(mine, key=lambda i: (_KIND_ORDER.get(i["kind"], 9) if _short(i) else 9, len(i["text"])))
            blocks.append({"type": "paragraph", "sentences": [_s(pick["text"], [pick["id"]], "copied")]})
        if blocks:
            sections.append({"heading": _CATEGORY_LABEL[cat], "blocks": blocks})
    if r:
        sections.append({"heading": "Scorecard", "blocks": [{"type": "scorecard"}]})
    gaps = [_s(g["text"], list(g["derived_from"])) for g in p.get("gap_summary", [])]
    if gaps:
        sections.append({"heading": "What we could not determine", "blocks": [{"type": "bullets", "sentences": gaps}]})
    return {"sections": sections}


def fake_judge(system: str, user: str, schema: dict) -> dict:
    """Offline judge: always supported. Tests that need a dissent register their own fake."""
    return {"supported": True, "unsupported_spans": [], "reason": "Offline fake judge (simulated): no model was called."}


def strip_spans(sentence: str, spans: list[str]) -> str:
    """Remove the flagged spans and tidy what is left: the mechanical weakening the offline repairer does."""
    out = sentence
    for span in sorted({s for s in spans if s}, key=len, reverse=True):
        out = out.replace(span, " ")
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+([,.;:])", r"\1", out)
    out = re.sub(r"(?:\s*(?:[,;:]|\b(?:and|but|while|with|as)\b))+\s*\.", ".", out)  # "million, and." -> "million."
    out = out.strip(" ,;:")
    if out and out[-1] not in ".!?":
        out += "."
    return out


def fake_rewrite(system: str, user: str, schema: dict) -> dict:
    """Offline repairer: drop the flagged spans. Delete when nothing changes or under four words are left."""
    task = _tagged(user, "task") or {}
    sentence = task.get("sentence", "")
    spans = [s for c in task.get("failed_checks", []) for s in c.get("unsupported_spans", [])]
    fixed = strip_spans(sentence, spans)
    if not spans or fixed == sentence or len(fixed.split()) < 4:
        return {"action": "delete", "text": ""}
    return {"action": "rewrite", "text": fixed}


register_fake(tool_name(BriefDraft), fake_brief)
register_fake(tool_name(FaithfulnessVerdict), fake_judge)
register_fake(tool_name(BriefRewrite), fake_rewrite)
