"""
Reference implementation of Axelrod's certainty score.

Pure functions over contract-shaped dicts, so the backend, the mock server and
any test can share one definition. The frontend only displays the result.

    strength     = support x inherited
    certainty    = strength x completeness   (shown as an integer 0-100)

    support      1.0 when code proves the item (exact quote, recomputed
                 numbers, chart built from refs). For semantic checks, the
                 product of judge agreement (votes_for / votes_total).
                 0 when the item failed, is blocked or still pending.
    inherited    Data items: the source's reliability score (0-1, set by the
                 planner, shown at approval). Everything else: the weakest
                 strength among the items it cites.
    completeness BOFFIN scores only: confidence high 1.0, medium 0.85,
                 low 0.6. 1.0 for everything else. Not inherited, so a
                 sentence that *describes* a data gap is not penalized for it.

Bands: high >= 85, medium 60-84, low < 60, none = failed, blocked, pending,
or an unscored (N/A) criterion.

Special refs in brief sentences:
- "rollup": strength = weakest strength among scored extrapolations.
- "HOLE-##": strength 1.0 (the gap itself is a known fact). When a sentence
  cites a failed item together with that item's hole, the sentence is about
  the gap, so the failed item is skipped.

This is a measure of how well each claim is supported by checked evidence.
It is not a probability that the claim is true. Say that in the UI tooltip.
"""
from __future__ import annotations

COMPLETENESS = {"high": 1.0, "medium": 0.85, "low": 0.6}
PASSING = ("pass", "fixed")


def band(score: int | None) -> str:
    if score is None:
        return "none"
    if score >= 85:
        return "high"
    if score >= 60:
        return "medium"
    return "low"


def _judges(verification: dict) -> tuple[float, str]:
    support, notes = 1.0, []
    for c in verification.get("checks", []):
        if c.get("method") == "llm_judge" and c.get("votes_total"):
            support *= c["votes_for"] / c["votes_total"]
            notes.append(f"judges {c['votes_for']}/{c['votes_total']}")
    return support, ", ".join(notes)


def _none(basis: str, strength: float = 0.0, weakest: str | None = None) -> dict:
    return {"score": None, "band": "none", "strength": round(strength, 4), "support": round(strength, 4),
            "inherited": 1.0 if strength else 0.0, "completeness": 1.0, "weakest_ref": weakest, "basis": basis}


def _result(support: float, inherited: float, completeness: float, weakest: str | None, basis: str) -> dict:
    strength = support * inherited
    score = round(100 * strength * completeness)
    return {"score": score, "band": band(score), "strength": round(strength, 4), "support": round(support, 4),
            "inherited": round(inherited, 4), "completeness": completeness, "weakest_ref": weakest, "basis": basis}


def item_certainties(items: list[dict], sources: list[dict]) -> dict[str, dict]:
    """Certainty for every D, C, E and G item, keyed by id."""
    by_id = {i["id"]: i for i in items}
    reliability = {s["source_id"]: s["scores"]["reliability"] for s in sources}
    out: dict[str, dict] = {}

    def refs_of(it: dict) -> list[str]:
        if it["kind"] == "conclusion":
            return it["data_ids"]
        if it["kind"] == "extrapolation":
            return it["conclusion_ids"]
        if it["kind"] == "chart":
            return it["refs"]
        return []

    def get(iid: str) -> dict:
        iid = iid.split(":")[0]  # "C-001:rotic_fy2025" -> "C-001"
        if iid in out:
            return out[iid]
        it = by_id[iid]
        v = it["verification"]
        if it["kind"] == "extrapolation" and it["score"] is None:
            out[iid] = _none(f"Not scored: {it.get('na_reason') or 'no reason given'}", strength=1.0)
            return out[iid]
        if v["status"] not in PASSING:
            out[iid] = _none(f"Verification {v['status']}")
            return out[iid]
        support, judge_note = _judges(v)
        if it["kind"] == "data":
            r = reliability[it["source_id"]]
            how = "Read by code from structured data" if it["extracted_by"] == "code" else "Exact quote"
            out[iid] = _result(support, r, 1.0, it["source_id"],
                               f"{how}, {it['source_id']} ({it['source_tier']}, reliability {r:.2f})")
            return out[iid]
        refs = refs_of(it)
        strengths = [(get(r)["strength"], r.split(":")[0]) for r in refs] or [(1.0, None)]
        inherited, weakest = min(strengths, key=lambda x: x[0])
        completeness = COMPLETENESS[it["confidence"]] if it["kind"] == "extrapolation" else 1.0
        parts = [judge_note or ("built by code from refs" if it["kind"] == "chart" else "code checks pass")]
        if weakest:
            parts.append(f"weakest input {weakest} ({inherited:.2f})")
        if it["kind"] == "extrapolation":
            parts.append(f"data coverage {it['confidence']} ({completeness:.2f})")
        out[iid] = _result(support, inherited, completeness, weakest, "; ".join(parts))
        return out[iid]

    for it in items:
        get(it["id"])
    return out


def sentence_certainty(sentence: dict, item_cert: dict[str, dict], items: list[dict], holes: list[dict]) -> dict:
    v = sentence["verification"]
    if v["status"] not in PASSING:
        return _none(f"Verification {v['status']}")
    by_id = {i["id"]: i for i in items}
    hole_items = {h["hole_id"]: h.get("item_id") for h in holes}
    cited_hole_items = {hole_items[r] for r in sentence["derived_from"] if r in hole_items}
    strengths = []
    for r in sentence["derived_from"]:
        if r == "rollup":
            scored = [item_cert[i["id"]]["strength"] for i in items
                      if i["kind"] == "extrapolation" and i["score"] is not None]
            strengths.append((min(scored) if scored else 0.0, "rollup"))
        elif r in hole_items:
            strengths.append((1.0, r))
        elif r in by_id:
            if by_id[r]["verification"]["status"] not in PASSING and r in cited_hole_items:
                continue  # the sentence is about the gap, and the hole is cited
            strengths.append((item_cert[r]["strength"], r))
        else:
            strengths.append((0.0, r))  # unknown ref: treat as unsupported
    inherited, weakest = min(strengths, key=lambda x: x[0]) if strengths else (0.0, None)
    support, judge_note = _judges(v)
    basis = "; ".join(x for x in [judge_note or ("copied verbatim" if sentence["mode"] == "copied" else "code checks pass"),
                                  f"weakest cited {weakest} ({inherited:.2f})" if weakest else ""] if x)
    return _result(support, inherited, 1.0, weakest, basis)


def report_certainty(entries: list[tuple[dict, int]], coverage_scored: int, coverage_in_scope: int, method: str) -> dict:
    """entries: (certainty, weight) pairs. Weight = word count for brief sentences, 1 for items."""
    total_w = sum(w for _, w in entries) or 1
    scored = [(c["score"], w) for c, w in entries if c["score"] is not None]
    mean = round(sum(s * w for s, w in scored) / sum(w for _, w in scored)) if scored else None
    count = {b: sum(1 for c, _ in entries if c["band"] == b) for b in ("high", "medium", "low", "none")}
    high_w = sum(w for c, w in entries if c["band"] == "high")
    return {"mean": mean, "high_pct": round(100 * high_w / total_w), **count,
            "coverage_scored": coverage_scored, "coverage_in_scope": coverage_in_scope, "method": method}
