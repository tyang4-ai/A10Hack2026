"""Stage draft: conclusions, BOFFIN scores and charts from verified data items.

In-scope criteria are grouped by the plan's needs (criteria that share a need share a drafter), and one
large-model drafter per group runs in parallel. Each sees only the verified data items that serve its
criteria. Code then turns the drafts into contract items:
- C, E and G ids come from code. The drafter's own keys ("c1") are only used to link scores and charts.
- Refs to data items that don't exist or didn't pass are dropped, and a conclusion left with none is dropped.
- Every computation is evaluated by code (verify.compute.safe_eval). Before the draft is final, code runs the
  verify stage's own code checks on it (precheck) and the drafter revises once with code's findings. A display
  in the first draft is kept as written, so a wrong one the drafter did not fix is caught by the computation
  check and repaired by recompute. A computation the drafter wrote or changed in its revision gets code's
  display (fill_displays): the drafter was already shown code's values once, and does arithmetic badly.
- Each metric group also gets code's inputs_by_year index and every row its expr_value, so the drafter
  computes from the years it has instead of refusing (B-fix-draft, after the R0 rehearsal).
- One claim per conclusion, at most two sentences and three numbers (B-fix-yield, after G5b, where one wrong
  number in a long paragraph took a whole criterion's support with it). Code counts both and sends a long
  claim back to be split in the same single revision, with computed rates that differ from a rate printed in
  the evidence. The pre-check also names a criterion with no score, and for a display that does not match
  code's value, the negative-reported id whose sign would explain it. Keys in a score's text become C ids.
- Chart points are computed by code from their refs. A model never draws a chart.
- Every in-scope criterion gets exactly one extrapolation. A criterion the drafter skipped, or one about the
  analyst rather than the company (research_stage), comes out N/A with a reason, which finalize declares
  as a hole. The data-coverage label can only be lowered by code, never raised.
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from typing import TYPE_CHECKING

from ..agents import drafter
from ..agents.base import AgentOutputError, AgentRunner
from ..contract import RUBRIC, m
from ..gateway import Blocked
from ..runtime import StopRequested
from ..verify import checks, compute, text as vtext
from ..verify.numbers import SCALES, half_up, period_key, scan_numbers, to_base
from .finalize import PASSING, in_scope, label

if TYPE_CHECKING:
    from ..runtime import RunContext
    from ..store.state import RunState

# Criteria about the analyst, not the company: no source can state them, so they are never scored by a model.
ANALYST_ONLY = {
    "research_stage": ("Research stage is about the analyst's own coverage history, which no source in this run "
                       "can state. It is declared as a gap instead of guessed."),
    "fundamental_thesis": ("Fundamental thesis is only meaningful against a prior review, and this run has none. "
                           "It is declared as a gap instead of guessed."),
}
_TEN_YEARS = re.compile(r"\b10\s?y\b|\b10[- ]years?\b|\bten years\b", re.IGNORECASE)
_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")
_BAR = re.compile(r"(>=|<=|≥|≤|>|<)\s*\d")
_TIER_RANK = {"priority": 0, "tier_2": 1, "tier_3": 2}
# Rows per drafter. A guard on prompt size only: the SHL ROTIC group (company and two peers, several years of
# income statement and balance sheet lines) is 159 rows, about 25k tokens.
MAX_ROWS = 400
_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
                 10: "ten"}
_LITERAL = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
SMALL_CONSTANT = 100      # whole numbers up to this may appear in an expr (averages, year counts, percent)
MAX_PROBLEMS = 30         # pre-check findings sent back to a drafter
MAX_SENTENCES = 2         # one claim per conclusion (B-fix-yield): at most two sentences ...
MAX_NUMBERS = 3           # ... and three numbers, fiscal-year labels and dates aside
MAX_ADVICE = 20           # one-claim and printed-rate notes sent back to a drafter


# ---------------------------------------------------------------------------
# Sentences and numbers in a drafted text (shared with stages/repair.py)
# ---------------------------------------------------------------------------

# A stop, closing quotes or brackets, then a space and what starts a sentence. Words before a stop that do not
# end a sentence: abbreviations and month names ("approx. 5", "Sep. 30"), and single letters ("U.S.").
_STOP = re.compile(r"[.!?][\"”’)\]]*(?=\s+[A-Z€$£\"“(\d])")
_WORD_BEFORE = re.compile(r"(?<![\w.])([A-Za-z][A-Za-z.]*)$")
_ABBREV = {"approx", "ca", "e.g", "i.e", "vs", "no", "nos", "inc", "co", "corp", "ltd", "plc", "st", "mr", "ms",
           "dr", "fig", "figs", "cf", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov",
           "dec", "est", "incl", "excl", "resp", "mn", "bn", "pp"}


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence in a drafted text, leading spaces left out. Repair removes whole sentences,
    so a boundary is only taken where it surely is one."""
    out: list[tuple[int, int]] = []
    start = 0
    for mm in _STOP.finditer(text):
        word = _WORD_BEFORE.search(text, start, mm.start())
        w = word.group(1).lower() if word else ""
        if w in _ABBREV or len(w) == 1 or re.fullmatch(r"(?:[a-z]\.)+[a-z]", w):
            continue
        out.append((start, mm.end()))
        start = mm.end()
    out.append((start, len(text)))
    spans = []
    for s, e in out:
        s += len(text[s:e]) - len(text[s:e].lstrip())
        if s < e:
            spans.append((s, e))
    return spans


def sentences(text: str) -> list[str]:
    return [text[s:e] for s, e in sentence_spans(text)]


def claim_size(text: str) -> tuple[int, int]:
    """(sentences, numbers) in a claim's text. Numbers are what numbers_traced checks: fiscal-year labels, years,
    dates and ids are not counted."""
    return len(sentence_spans(text)), len(scan_numbers(vtext.normalize(text)))


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _source_ok(src: m.Source | None) -> bool:
    return src is not None and src.status not in ("revoked", "quarantined") and src.approval != "rejected"


def usable_data(state: RunState) -> list[m.DataItem]:
    """Verified data items whose source still stands. Nothing else reaches a drafter."""
    srcs = state.sources_by_id()
    return [d for d in state.data_items() if d.verification.status in PASSING and _source_ok(srcs.get(d.source_id))]


def criterion_groups(scope: list[str], needs: list[m.DataNeed]) -> list[list[str]]:
    """Union of in-scope criteria that share a need, in scope order. A criterion no need names stands alone."""
    parent = {c: c for c in scope}

    def find(c: str) -> str:
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    for need in needs:
        crit = [c for c in need.criteria if c in parent]
        for c in crit[1:]:
            a, b = find(crit[0]), find(c)
            if a != b:
                parent[max(a, b, key=scope.index)] = min(a, b, key=scope.index)
    groups: dict[str, list[str]] = {}
    for c in scope:
        groups.setdefault(find(c), []).append(c)
    return sorted(groups.values(), key=lambda g: scope.index(g[0]))


def plain(x: Decimal) -> str:
    """A Decimal as a plain number string, no exponent: 403000000, -0.21, 2.5."""
    s = format(x.normalize(), "f")
    return "0" if s in ("-0", "0") else s


def data_row(d: m.DataItem) -> dict:
    """What a drafter sees of one data item: no quotes, no offsets. expr_value is what the id stands for in an
    expression (checks.base_value: base units, sign as reported, a percentage as a fraction), computed by code
    so the drafter never has to guess a sign or a scale."""
    v = d.value
    row = {"id": d.id, "statement": d.statement, "tier": d.source_tier, "criteria": list(d.criteria)}
    if v is not None:
        row.update({"value": v.as_reported, "unit": v.unit, "scale": v.scale, "period": v.period,
                    "expr_value": plain(checks.base_value(d))})
    return row


def group_items(data: list[m.DataItem], group: list[str], limit: int = MAX_ROWS) -> tuple[list[m.DataItem], int]:
    """The items a group's drafter sees: verified items serving any of its criteria, in id order, the best tiers
    kept when there are more than the cap. Returns them and how many the cap left out (0 for SHL: its largest
    group has 159)."""
    mine = [d for d in data if set(d.criteria) & set(group)]
    ranked = sorted(range(len(mine)), key=lambda i: (_TIER_RANK.get(mine[i].source_tier, 3), i))
    keep = set(ranked[:limit])
    return [d for i, d in enumerate(mine) if i in keep], len(mine) - len(keep)


def group_rows(data: list[m.DataItem], group: list[str], limit: int = MAX_ROWS) -> tuple[list[dict], int]:
    """group_items as drafter rows."""
    items, left = group_items(data, group, limit)
    return [data_row(d) for d in items], left


# Line items of the rubric's NOPAT and TICAP definitions, found by words in a data item's statement. First match
# wins, so "income before income taxes" is checked before "income tax".
_CONCEPTS = (
    ("pretax_income", ("income before income taxes", "profit before tax")),
    ("income_tax", ("income tax",)),
    ("ebit", ("earnings before interest and taxes", "operating income", "income from operations")),
    ("amortization", ("purchase price allocation", "amortization of intangible", "amortization of acquired",
                      "intangible assets acquired")),
    ("nci_income", ("income attributable to non-controlling", "non-controlling interests attributable",
                    "non-controlling interests were")),
    ("total_assets", ("total assets",)),
    ("cash", ("cash and cash equivalents",)),
    ("goodwill", ("goodwill",)),
    ("trade_payables", ("trade payables", "accounts payable")),
    ("current_liabilities", ("total current liabilities",)),
    ("short_term_debt", ("short-term debt", "short-term financial debt")),
)
_NOT_A_CONCEPT = ("adjusted", "segment", "margin", "capitalized development", "siemens group", " due in ")


def concept_of(statement: str) -> str | None:
    """Which NOPAT or TICAP line item a statement reports, or None. A navigation aid only: the drafter still
    reads every statement and cites ids, and code checks every computation."""
    s = statement.lower()
    if any(w in s for w in _NOT_A_CONCEPT):
        return None
    for concept, words in _CONCEPTS:
        if any(w in s for w in words):
            if concept == "nci_income" and (" as of " in s or " at september" in s or " at dec" in s):
                return None          # the equity balance, not the income line
            return concept
    return None


_PEER_IN = re.compile(r"\(peer:\s*([^)]+)\)", re.IGNORECASE)        # "company facts (peer: Harbor Scientific)"
_PEER_AFTER = re.compile(r":\s*([^:]+?)\s*\(peer\)\s*$", re.IGNORECASE)   # "company facts: GE HealthCare (peer)"


def company_of(src: m.Source | None, company: str) -> str:
    """The company a source reports on: the peer its title names when the plan marked it as a peer source,
    otherwise the run's own company."""
    title = (src.title if src is not None else "") or ""
    mm = _PEER_IN.search(title) or _PEER_AFTER.search(title)
    return mm.group(1).strip() if mm else company


def inputs_index(state: RunState, items: list[m.DataItem]) -> list[dict]:
    """For metric groups: one line per company and fiscal year naming the data ids that hold each NOPAT and
    TICAP line item, built by code so the drafter sees at a glance which years can be computed and pairs
    balances from the right year-ends (the R0 drafter mixed years up)."""
    srcs = state.sources_by_id()
    table: dict[tuple[str, int], dict[str, list[str]]] = {}
    for d in items:
        v = d.value
        concept = concept_of(d.statement) if v is not None and v.period else None
        years = period_key(v.period)[0] if concept else set()
        if len(years) != 1:
            continue
        who = company_of(srcs.get(d.source_id), state.run.company)
        line = table.setdefault((who, years.pop()), {})
        line.setdefault(concept, []).append(d.id)
        if checks.base_value(d) < 0:
            line.setdefault("reported_negative", []).append(d.id)
    order = [c for c, _ in _CONCEPTS] + ["reported_negative"]
    first = state.run.company
    return [{"company": who, "fiscal_year": year, **{k: found[k] for k in order if k in found}}
            for (who, year), found in sorted(table.items(), key=lambda kv: (kv[0][0] != first, kv[0][0], kv[0][1]))]


def years_asked(criterion: str) -> int | None:
    """10 for a criterion whose label or hint asks for ten years of data, else None."""
    crit = RUBRIC["criteria"][criterion]
    return 10 if _TEN_YEARS.search(f"{crit['label']} {crit.get('evidence_hint', '')}") else None


def criterion_entry(criterion: str) -> dict:
    crit = RUBRIC["criteria"][criterion]
    out = {"criterion": criterion, "label": crit["label"], "category": crit["category"], "scoring": crit["scoring"],
           "plus_one": crit.get("plus_one"), "minus_one": crit.get("minus_one"),
           "evidence_hint": crit.get("evidence_hint")}
    if crit.get("zero"):
        out["zero"] = crit["zero"]
    if crit["scoring"] == "threshold" and _BAR.search(crit.get("plus_one") or "") and _BAR.search(crit.get("minus_one") or ""):
        out["numeric_bar"] = True
    if years_asked(criterion):
        out["years_asked"] = years_asked(criterion)
        out["short_series"] = ("Compute every fiscal year the data allows and score on those. Fewer years means "
                               "low confidence, not N/A.")
    if criterion in ANALYST_ONLY:
        out["not_scored"] = True
        out["not_scored_reason"] = ANALYST_ONLY[criterion]
    return out


def metrics_for(group: list[str]) -> dict | None:
    """The rubric's metric definitions, for groups that compute returns."""
    if any("rotic" in c for c in group):
        return {k: v for k, v in RUBRIC.get("metrics", {}).items()}
    return None


def company_view(state: RunState) -> dict:
    return {"name": state.run.company, "ticker": state.run.ticker, "depth": state.run.depth,
            "as_of": state.as_of, "fiscal_note": state.fiscal_note}


def needs_view(needs: list[m.DataNeed], group: list[str]) -> list[dict]:
    """The approved plan's needs for these criteria (question and rationale), so the drafter knows which inputs
    were collected (the R0 drafter called the PPA amortization missing although it was in its data)."""
    return [{"need_id": n.need_id, "label": n.label, "question": n.question, "rationale": n.rationale}
            for n in needs if set(n.criteria) & set(group)]


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def typed_amounts(expr: str) -> list[str]:
    """Plain numbers in an expr other than small whole constants (2 for an average, 5 for five years, 100):
    a typed-in amount or rate has no source, so every input must come from a data id instead."""
    out = []
    for raw in _LITERAL.findall(_ID.sub(" ", expr)):
        x = Decimal(raw)
        if x != x.to_integral_value() or x > SMALL_CONSTANT:
            out.append(raw)
    return out


def _cited(dc: drafter.DraftConclusion, verified: dict[str, m.DataItem], shown: set[str]) -> list[str]:
    ids = dict.fromkeys(dc.data_ids + [i for c in dc.computations for i in compute.ids_in(c.expr)])
    return [i for i in ids if i in verified and i in shown]


def _swap(text: str, names: dict[str, str]) -> str:
    for tid, key in names.items():
        text = text.replace(tid, key)
    return text


UNTRACED_FIX = ("Each of these numbers is neither a cited item's value nor a computation's display. Declare a "
                "computation and use its display, or reword without the number. Formulas belong in expr, not in "
                "the text: describe the method in words (\"less tax at the effective rate\", not \"x (1 - t)\").")


def _where(r: m.CheckResult, text: str) -> dict:
    """For an untraced-number failure: where each number sits in the text, and how to fix it."""
    if r.check != "numbers_traced" or not r.unsupported_spans:
        return {}
    spots = []
    for span in dict.fromkeys(r.unsupported_spans):
        for mm in re.finditer(re.escape(span), text):
            spots.append(f"...{text[max(0, mm.start() - 40):mm.end() + 25]}...")
            if len(spots) >= 8:
                break
    return {"where": spots, "fix": UNTRACED_FIX}


def _exact(r: m.CheckResult, rule_input: str | None, items: dict) -> dict:
    """For a threshold failure: the unrounded value code compared with the bar, since a display such as
    "10.0%" can hide a value just under the 10% bar."""
    cid, _, name = (rule_input or "").partition(":")
    if r.check != "threshold_rule" or cid not in items or not name:
        return {}
    try:
        x = checks.conclusion_env(items[cid], items).get(name)
    except compute.ComputeError:
        return {}
    return {} if x is None else {"exact_value": plain(x.quantize(Decimal("1e-8")))}


def _shown(value: Decimal, display: str) -> str:
    from .repair import redisplay      # repair imports this module, so import late

    return redisplay(value, display) or plain(value)


def sign_hints(dc: drafter.DraftConclusion, verified: dict[str, m.DataItem], cited: list[str]) -> list[str]:
    """For a computation whose display does not match code's value and that reads a data id reported as a
    negative number: what it gives with that id taken as positive. In G5b the drafter's displays had the right
    sign in mind while its exprs added SHL's negative tax expense and capex back (C-001, C-008)."""
    env = {i: checks.base_value(verified[i]) for i in cited if verified[i].value is not None}
    out: list[str] = []
    for comp in dc.computations:
        if typed_amounts(comp.expr):
            continue
        try:
            val = compute.safe_eval(comp.expr, env)
        except compute.ComputeError:
            continue
        meant = _display_value(comp.display)
        if meant is not None and not checks.display_of(val, comp.display)[0]:
            best: tuple[Decimal, str, Decimal] | None = None
            for did in dict.fromkeys(compute.ids_in(comp.expr)):
                if did not in env or env[did] >= 0:
                    continue
                try:
                    alt = compute.safe_eval(comp.expr, {**env, did: -env[did]})
                except compute.ComputeError:
                    continue
                gap = abs(alt - meant)
                if gap < abs(val - meant) and (best is None or gap < best[0]):
                    best = (gap, did, alt)
            if best is not None:
                _, did, alt = best
                out.append(f"{comp.name} reads {did}, which is reported as a negative number (expr_value "
                           f"{plain(env[did])}). Code gets {_shown(val, comp.display)} where your display says "
                           f"{comp.display}. With {did} taken as positive it would give {_shown(alt, comp.display)}. "
                           f"Check its sign: adding a negative-reported amount deducts it, subtracting it adds it back.")
        env[comp.name] = val
    return out[:6]


def _display_value(display: str) -> Decimal | None:
    """What a display says in expr units: "24.2%" is 0.242, "€1,234.5 million" is 1234500000."""
    toks = scan_numbers(vtext.normalize(display), include_labels=True)
    if not toks:
        return None
    return toks[0].value / 100 if toks[0].pct else toks[0].value


def printed_conflict(comp: drafter.DraftComputation | m.Computation, verified: dict[str, m.DataItem],
                     shown: set[str]) -> str | None:
    """A computed percentage that differs by a rounding step from a percentage printed in the evidence quote of
    an item the computation reads. G5b C-003: Varian growth computed as 5.6% from rounded revenue, while the
    revenue row prints 5.5%, so all three judges rejected the claim."""
    toks = scan_numbers(vtext.normalize(comp.display), include_labels=True)
    if not toks or not toks[0].pct:
        return None
    t = toks[0]
    near = Decimal("1.5") * Decimal(10) ** -t.places
    for did in dict.fromkeys(compute.ids_in(comp.expr)):
        d = verified.get(did)
        if d is None or did not in shown:
            continue
        for cit in d.citations:
            if cit.role != "evidence" or not cit.quote:
                continue
            for p in scan_numbers(vtext.normalize(cit.quote)):
                if p.pct and p.number != t.number and abs(p.number - t.number) <= near:
                    return (f"{did}'s source line prints {p.raw} next to these figures, and your computed display "
                            f"is {comp.display}.")
    return None


def advice(out: drafter.DraftOutput, verified: dict[str, m.DataItem], shown: set[str]) -> list[dict]:
    """Code's notes on a draft that are not check failures, sent back with the revision in their own block:
    conclusions over the one-claim size, and computed rates the source prints differently."""
    notes: list[dict] = []
    for dc in out.conclusions:
        key = dc.key.strip()
        n_sent, n_num = claim_size(dc.text)
        if n_sent > MAX_SENTENCES or n_num > MAX_NUMBERS:
            notes.append({"conclusion": key, "sentences": n_sent, "numbers": n_num,
                          "problem": f"{n_sent} sentence(s) and {n_num} number(s). One claim is at most "
                                     f"{MAX_SENTENCES} sentences and {MAX_NUMBERS} numbers.",
                          "fix": "Split it into small conclusions under new keys, and add the new keys to the "
                                 "scores that cite it."})
        for comp in dc.computations:
            found = printed_conflict(comp, verified, shown)
            if found:
                notes.append({"conclusion": key, "computation": comp.name, "problem": found,
                              "fix": "Leave the computed rate out of the text, or state only the reported figures."})
    return notes


def missing_scores(group: list[str], out: drafter.DraftOutput) -> list[dict]:
    """Criteria the draft gave no score. In G5b the ROTIC drafter's first answer had none, its revision sent only
    rotic_level, and rotic_vs_peers came out N/A for that alone."""
    have = {s.criterion for s in out.scores}
    return [{"score": c, "check": "score_missing",
             "problem": f"No score for {c}. Send one: -1, 0 or 1 under the rubric, or null with na_reason."}
            for c in group if c not in have and c not in ANALYST_ONLY]


def precheck(state: RunState, group: list[str], out: drafter.DraftOutput, verified: dict[str, m.DataItem],
             shown: set[str]) -> list[dict]:
    """Code's own checks on a draft before it is final, the same ones the verify stage runs: every computation
    evaluates from shown verified data with no typed amounts, every display matches code's value, every number
    in a text traces, and a threshold score agrees with its bar. Problems are keyed by the drafter's own labels.
    The drafter gets one chance to fix them (see _draft_group); the verify stage checks everything again."""
    items: dict = dict(state.items)
    sources = state.sources_by_id()
    tids: dict[str, str] = {}               # drafter key -> temporary id
    found: list[dict] = []
    for n, dc in enumerate(out.conclusions, 1):
        key = dc.key.strip()
        cited = _cited(dc, verified, shown)
        if not cited:
            found.append({"conclusion": key, "problem": "cites no verified data id from <data>"})
            continue
        env = {i: checks.base_value(verified[i]) for i in cited if verified[i].value is not None}
        comps = []
        for comp in dc.computations:
            typed = typed_amounts(comp.expr)
            if typed:
                found.append({"conclusion": key, "computation": comp.name, "check": "computation",
                              "problem": f"{comp.expr!r} types {', '.join(typed)}. Use the data id that holds the "
                                         f"number, so every input traces to a source."})
                continue
            try:
                val = compute.safe_eval(comp.expr, env)
            except compute.ComputeError as e:
                found.append({"conclusion": key, "computation": comp.name, "check": "computation", "problem": str(e)})
                continue
            env[comp.name] = val
            comps.append(m.Computation(name=comp.name, expr=comp.expr, result=float(val), display=comp.display))
        tid = f"C-9{n:02d}"
        crit = [c for c in dc.criteria if c in group] or [group[0]]
        items[tid] = m.Conclusion(id=tid, category=RUBRIC["criteria"][crit[0]]["category"], criteria=crit,
                                  text=dc.text or "-", data_ids=cited, computations=comps,
                                  verification=m.Verification(status="pass"))
        tids[key] = tid
        for r in checks.check_conclusion(items[tid], items, sources, rubric=RUBRIC):
            if r.result == "fail" and r.check != "citations_resolve":
                hints = sign_hints(dc, verified, cited) if r.check == "computation" else []
                found.append({"conclusion": key, "check": r.check, "problem": r.detail, **_where(r, dc.text),
                              **({"sign_check": hints} if hints else {})})
    found += missing_scores(group, out)
    names = {tid: key for key, tid in tids.items()}
    for ds in out.scores:
        if ds.criterion not in group or ds.score is None or ds.criterion in ANALYST_ONLY:
            continue
        crit = RUBRIC["criteria"][ds.criterion]
        key, _, cname = (ds.rule_input or "").partition(":")
        rule_input = f"{tids[key.strip()]}:{cname.strip()}" if key.strip() in tids else ds.rule_input
        cids = list(dict.fromkeys([tids[k.strip()] for k in ds.conclusion_keys if k.strip() in tids]
                                  + ([rule_input.split(":")[0]] if key.strip() in tids else [])))
        e = m.Extrapolation(id="E-900", category=crit["category"], criterion=ds.criterion, score=ds.score,
                            text=ds.text or "-", conclusion_ids=cids, confidence=ds.confidence,
                            rule=m.ScoreRule(type=crit["scoring"], plus_one=crit.get("plus_one"),
                                             minus_one=crit.get("minus_one"), input=rule_input),
                            verification=m.Verification(status="pending"))
        for r in checks.check_extrapolation(e, items, RUBRIC, sources=sources):
            if r.result == "fail":
                found.append({"score": ds.criterion, "check": r.check, "problem": _swap(r.detail, names),
                              **_where(r, ds.text), **_exact(r, rule_input, items)})
    return [{k: _swap(v, names) if isinstance(v, str) else v for k, v in p.items()} for p in found]


async def _draft_group(ctx: RunContext, group: list[str], rows: list[dict],
                       verified: dict[str, m.DataItem] | None = None,
                       index: list[dict] | None = None) -> tuple[drafter.DraftOutput | None, str]:
    """One drafter for one group. Returns the draft, or None and why. When code's pre-check finds problems in
    the draft, the same drafter revises it once with code's findings (and code's values for wrong displays);
    the revision is kept only if it validates."""
    prompt = drafter.draft_prompt(company_view(ctx.state), [criterion_entry(c) for c in group], rows,
                                  metrics_for(group), needs_view(ctx.state.needs, group), index)
    runner = AgentRunner(ctx, "drafter", f"Draft {', '.join(group)}", [r["id"] for r in rows], "large")
    shown = {r["id"] for r in rows}
    try:
        with runner:
            out = await runner.ask(drafter.SYSTEM, prompt, drafter.DraftOutput, max_tokens=drafter.MAX_TOKENS)
            problems = precheck(ctx.state, group, out, verified or {}, shown) if verified is not None else []
            notes = advice(out, verified or {}, shown) if verified is not None else []
            if problems or notes:
                out = await _revise(ctx, runner, group, prompt, out, problems, verified or {}, shown, notes)
    except StopRequested:
        raise
    except Blocked as e:
        return None, f"The drafter was blocked by the gateway: {e.reason}"
    except AgentOutputError:
        return None, "The drafter's output failed validation twice."
    return out, runner.agent_id  # type: ignore[return-value]


def merge_revision(old: drafter.DraftOutput, new: drafter.DraftOutput) -> drafter.DraftOutput:
    """The revision sends only what changed: conclusions by key (an empty text drops one), scores by criterion,
    charts by title (no series drops one). Everything it leaves out stays as drafted."""
    conclusions = {c.key.strip(): c for c in old.conclusions}
    for c in new.conclusions:
        if c.text.strip():
            conclusions[c.key.strip()] = c
        else:
            conclusions.pop(c.key.strip(), None)
    scores = {s.criterion: s for s in old.scores}
    scores.update({s.criterion: s for s in new.scores})
    charts = {g.title.strip(): g for g in old.charts}
    for g in new.charts:
        if g.series:
            charts[g.title.strip()] = g
        else:
            charts.pop(g.title.strip(), None)
    return drafter.DraftOutput(conclusions=list(conclusions.values()), scores=list(scores.values()),
                               charts=list(charts.values()))


async def _revise(ctx: RunContext, runner: AgentRunner, group: list[str], prompt: str, out: drafter.DraftOutput,
                  problems: list[dict], verified: dict[str, m.DataItem], shown: set[str],
                  notes: list[dict] | None = None) -> drafter.DraftOutput:
    """One revision round, for check problems, advice notes (claims to split, printed rates) or both. A revision
    that is blocked or invalid leaves the first draft in place."""
    aid = runner.agent_id
    notes = notes or []
    if problems:
        ctx.emit("agent.log", m.AgentLogData(text=f"Code pre-check found {len(problems)} problem(s) in the draft, "
                                                  f"sent back once: "
                                                  + "; ".join(p["problem"][:160] for p in problems[:6])),
                 message=f"{aid}: code pre-check found {len(problems)} problem(s), drafter revises once",
                 agent_id=aid, level="warn")
    if notes:
        ctx.emit("agent.log", m.AgentLogData(text=f"Code advice on {len(notes)} part(s) of the draft (claims to split "
                                                  f"into small conclusions, printed rates), sent back once: "
                                                  + "; ".join(f"{n['conclusion']}: {n['problem'][:120]}"
                                                              for n in notes[:6])),
                 message=f"{aid}: code advice on {len(notes)} part(s), drafter revises once", agent_id=aid)
    try:
        new = await runner.ask(drafter.SYSTEM, drafter.revise_prompt(prompt, out.model_dump(mode="json"),
                                                                     problems[:MAX_PROBLEMS], notes[:MAX_ADVICE]),
                               drafter.DraftOutput, max_tokens=drafter.MAX_TOKENS)
    except (Blocked, AgentOutputError) as e:
        ctx.emit("agent.log", m.AgentLogData(text=f"Revision not used ({type(e).__name__}), first draft kept"),
                 message=f"{aid}: revision not used, first draft kept", agent_id=aid, level="warn")
        return out
    new = merge_revision(out, new)
    before = {(c.key.strip(), x.name, x.expr, x.display) for c in out.conclusions for x in c.computations}
    new, filled = fill_displays(new, verified, shown, skip=before)
    if filled:
        ctx.emit("agent.log", m.AgentLogData(text="Code filled the display of computations written in the revision: "
                                                  + "; ".join(filled)),
                 message=f"{aid}: code filled {len(filled)} display(s) in the revision", agent_id=aid)
    left = precheck(ctx.state, group, new, verified, shown)
    still = (f", {len(advice(new, verified, shown))} of {len(notes)} advice note(s) left" if notes else "")
    ctx.emit("agent.log", m.AgentLogData(text=f"Revised draft: {len(left)} pre-check problem(s) left of "
                                              f"{len(problems)}{still}. The verify stage checks everything again."),
             message=f"{aid}: revised draft, {len(left)} of {len(problems)} pre-check problem(s) left{still}",
             agent_id=aid)
    return new


def fill_displays(out: drafter.DraftOutput, verified: dict[str, m.DataItem], shown: set[str], *,
                  skip: set[tuple[str, str, str, str]] = frozenset()) -> tuple[drafter.DraftOutput, list[str]]:
    """Code's display, in the drafter's own format, for every computation whose display does not match its value,
    swapped into the conclusion's text and into the texts of the scores that cite it. Computations in skip (key,
    name, expr, display: the first draft's, sent back unchanged) stay as written for the checks to catch, and so
    does a sign the format cannot show. Returns the draft and what changed."""
    from .repair import redisplay, replace_number      # repair imports this module, so import late

    swaps: dict[str, list[tuple[str, str]]] = {}
    notes: list[str] = []
    conclusions = []
    for dc in out.conclusions:
        key = dc.key.strip()
        cited = _cited(dc, verified, shown)
        env = {i: checks.base_value(verified[i]) for i in cited if verified[i].value is not None}
        text, comps = dc.text, []
        # A display two computations share can't be swapped in the text: code can't tell whose mention is whose
        # (G5b C-001: FY2025 and FY2026 ROTIC were both written 10.7%). Those stay for the checks to catch.
        shared = {d for d in (c.display for c in dc.computations)
                  if sum(c.display == d for c in dc.computations) > 1}
        for comp in dc.computations:
            new_display = None
            if not typed_amounts(comp.expr):
                try:
                    env[comp.name] = val = compute.safe_eval(comp.expr, env)
                except compute.ComputeError:
                    val = None
                if (val is not None and (key, comp.name, comp.expr, comp.display) not in skip
                        and comp.display not in shared and not checks.display_of(val, comp.display)[0]):
                    new_display = redisplay(val, comp.display)
            if new_display is None:
                comps.append(comp)
                continue
            text = replace_number(text, comp.display, new_display)
            swaps.setdefault(key, []).append((comp.display, new_display))
            notes.append(f"{key}:{comp.name} {comp.display} -> {new_display}")
            comps.append(comp.model_copy(update={"display": new_display}))
        conclusions.append(dc.model_copy(update={"text": text, "computations": comps}))
    scores = []
    for ds in out.scores:
        text = ds.text
        keys = [k.strip() for k in ds.conclusion_keys] + [(ds.rule_input or "").partition(":")[0].strip()]
        for k in dict.fromkeys(keys):
            for old, new in swaps.get(k, []):
                text = replace_number(text, old, new)
        scores.append(ds.model_copy(update={"text": text}))
    return out.model_copy(update={"conclusions": conclusions, "scores": scores}), notes


def point_value(ref: str, unit: str, scale: str, items: dict) -> Decimal | None:
    """A chart point's value from its ref, in the chart's unit and scale: a data item's value, or a conclusion's
    computation recomputed by code. None when the ref has no value in that unit."""
    base, _, name = ref.partition(":")
    it = items.get(base)
    if it is None:
        return None
    if not name:
        if it.kind != "data" or it.value is None:
            return None
        v = it.value
        if unit == "%":
            return _dec(v.number) if v.unit == "%" else None
        if v.unit != unit:
            return None
        return to_base(_dec(v.number), v.scale) / SCALES[scale]
    if it.kind != "conclusion":
        return None
    try:
        x = checks.conclusion_env(it, items).get(name)
    except compute.ComputeError:
        return None
    if x is None:
        return None
    return x * 100 if unit == "%" else x / SCALES[scale]


class Builder:
    """Turns drafts into contract items. Code assigns every id, evaluates every computation and computes
    every chart point, and records what it rejected."""

    def __init__(self, ctx: RunContext, verified: dict[str, m.DataItem]) -> None:
        self.ctx = ctx
        self.verified = verified
        self.conclusions: dict[str, m.Conclusion] = {}
        self.keys: dict[tuple[int, str], str] = {}
        self.notes: dict[int, list[str]] = {}
        self.agent_ids: dict[int | None, str] = {}     # group index -> drafter agent id
        self.seen: dict[int, set[str]] = {}             # group index -> data ids its drafter was shown
        self.info: list[str] = []                       # what code noted without changing anything

    def note(self, gi: int, text: str) -> None:
        self.notes.setdefault(gi, []).append(text)

    # -- conclusions ---------------------------------------------------------------

    def _computations(self, gi: int, key: str, dc: drafter.DraftConclusion,
                      data_ids: list[str]) -> list[m.Computation]:
        env: dict[str, Decimal] = {i: checks.base_value(self.verified[i]) for i in data_ids
                                   if self.verified[i].value is not None}
        out: list[m.Computation] = []
        for comp in dc.computations:
            name = comp.name.strip()
            if not _NAME.match(name) or name in env:
                self.note(gi, f"{key}: computation {name!r} dropped (not a fresh snake_case name)")
                continue
            typed = typed_amounts(comp.expr)
            if typed:
                self.note(gi, f"{key}: computation {name} dropped (it types {', '.join(typed)} instead of citing "
                              f"the data id that holds the number)")
                continue
            try:
                val = compute.safe_eval(comp.expr, env)
            except compute.ComputeError as e:
                self.note(gi, f"{key}: computation {name} dropped ({e})")
                continue
            env[name] = val
            out.append(m.Computation(name=name, expr=comp.expr, result=float(val), display=comp.display.strip()))
        return out

    def conclusion(self, gi: int, group: list[str], dc: drafter.DraftConclusion) -> m.Conclusion | None:
        key = dc.key.strip() or f"#{len(self.keys) + 1}"
        cited = list(dict.fromkeys(dc.data_ids + [i for c in dc.computations for i in compute.ids_in(c.expr)]))
        shown = self.seen.get(gi, set())
        data_ids = [i for i in cited if i in self.verified and i in shown]
        dropped = [i for i in cited if i not in data_ids]
        if dropped:
            self.note(gi, f"{key}: dropped {', '.join(dropped)} (not a verified data item shown to this drafter)")
        if not data_ids:
            self.note(gi, f"{key}: conclusion dropped, it cites no verified data")
            return None
        if not dc.text.strip():
            self.note(gi, f"{key}: conclusion dropped, it has no text")
            return None
        crit = [c for c in dict.fromkeys(dc.criteria) if c in group] or [group[0]]
        cid = self.ctx.next_id("C")
        c = m.Conclusion(id=cid, category=RUBRIC["criteria"][crit[0]]["category"], criteria=crit, text=dc.text.strip(),
                         data_ids=data_ids, computations=self._computations(gi, key, dc, data_ids),
                         agent_id=self.agent_ids.get(gi), verification=m.Verification(status="pending"))
        c, fixed = fix_currencies(c, self.verified)
        for note in fixed:
            self.note(gi, f"{key}: {note}")
        self.keys.setdefault((gi, key), cid)
        self.conclusions[cid] = c
        return c

    # -- extrapolations --------------------------------------------------------------

    def _mapped(self, gi: int, keys: list[str]) -> list[str]:
        out = []
        for k in keys:
            cid = self.keys.get((gi, k.strip()))
            if cid is None:
                self.note(gi, f"score cites unknown conclusion key {k!r}, dropped")
            elif cid not in out:
                out.append(cid)
        return out

    def _input(self, gi: int, criterion: str, rule_input: str | None, cids: list[str]) -> str | None:
        if RUBRIC["criteria"][criterion]["scoring"] != "threshold" or not rule_input:
            return None
        key, _, name = rule_input.partition(":")
        cid = self.keys.get((gi, key.strip()))
        if cid is None or name.strip() not in {c.name for c in self.conclusions[cid].computations}:
            self.note(gi, f"{criterion}: rule input {rule_input!r} names no computation, dropped")
            return None
        if cid not in cids:
            cids.append(cid)
        return f"{cid}:{name.strip()}"

    def _years(self, cids: list[str], rule_input: str | None) -> set[int]:
        """Fiscal years of the data behind a score. With a rule input, only the data that computation reads
        (directly or through earlier computations), since that is the number code compares with the bar.
        Otherwise every cited conclusion's data."""
        ids: list[str] = []
        cid, _, name = (rule_input or "").partition(":")
        if cid in self.conclusions and name:
            ids = inputs_of(self.conclusions[cid], name)
        if not ids:
            ids = [did for c in cids for did in self.conclusions[c].data_ids]
        years: set[int] = set()
        for did in ids:
            v = self.verified[did].value if did in self.verified else None
            if v is not None and v.period:
                years |= period_key(v.period)[0]
        return years

    def _coverage(self, criterion: str, confidence: str, cids: list[str], rule_input: str | None = None, *,
                  scored: bool = True) -> tuple[str, str | None]:
        """Code may lower the data-coverage label, never raise it: a 10-year criterion with fewer than ten
        fiscal years of data behind it is low coverage. For a score, also notes which years it rests on."""
        asked = years_asked(criterion)
        if asked is None:
            return confidence, None
        years = self._years(cids, rule_input) if cids else set()
        span = f"{_count(len(years))} of {_count(asked)} fiscal years" + (
            f": {', '.join(str(y) for y in sorted(years))}" if years else "")
        if scored:
            self.info.append(f"{criterion}: the score rests on data from {span}")
        if len(years) >= asked or confidence == "low":
            return confidence, None
        return "low", f"{criterion}: data coverage lowered from {confidence} to low ({span})"

    def extrapolation(self, criterion: str, gi: int | None, ds: drafter.DraftScore | None, missing: str) -> m.Extrapolation:
        crit = RUBRIC["criteria"][criterion]
        rule = m.ScoreRule(type=crit["scoring"], plus_one=crit.get("plus_one"), minus_one=crit.get("minus_one"))
        base = {"id": self.ctx.next_id("E"), "category": crit["category"], "criterion": criterion, "rule": rule,
                "verification": m.Verification(status="pending"), "agent_id": self.agent_ids.get(gi)}
        cids = self._mapped(gi, ds.conclusion_keys) if (ds is not None and gi is not None) else []
        if criterion in ANALYST_ONLY:
            return m.Extrapolation(**base, score=None, na_reason=ANALYST_ONLY[criterion], text=f"{crit['label']} is not scored.",
                                   conclusion_ids=cids, confidence="low")
        if ds is None:
            return m.Extrapolation(**base, score=None, na_reason=missing, text=f"{crit['label']} is not scored.",
                                   conclusion_ids=[], confidence="low")
        rule.input = self._input(gi, criterion, ds.rule_input, cids) if gi is not None else None
        confidence, lowered = self._coverage(criterion, ds.confidence, cids, rule.input, scored=ds.score is not None)
        if lowered and gi is not None:
            self.note(gi, lowered)
        na = (ds.na_reason or "").strip() or None
        text = self.key_ids(gi, ds.text.strip()) if gi is not None else ds.text.strip()
        return m.Extrapolation(**base, score=ds.score, na_reason=na if ds.score is None else None,
                               text=text or f"{crit['label']} is not scored.", conclusion_ids=cids,
                               confidence=confidence)

    def key_ids(self, gi: int, text: str) -> str:
        """The drafter's conclusion keys in a score's text ("(c2)") become the conclusions' ids, so readers and
        judges can follow them, and repair can drop the sentence that rests on a conclusion that failed."""
        keys = {k: cid for (g, k), cid in self.keys.items() if g == gi and _KEY.fullmatch(k)}
        if not keys:
            return text
        alt = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
        return re.sub(rf"(?<![\w-])({alt})(?![\w-])", lambda mm: keys[mm.group(1)], text)

    # -- charts --------------------------------------------------------------------------

    def _resolve_ref(self, gi: int, ref: str) -> str | None:
        ref = ref.strip()
        if ":" not in ref:
            return ref if ref in self.verified and ref in self.seen.get(gi, set()) else None
        key, _, name = ref.partition(":")
        cid = self.keys.get((gi, key.strip()))
        return f"{cid}:{name.strip()}" if cid else None

    def _unit_scale(self, refs: list[str], hint_unit: str, hint_scale: str) -> tuple[str, str]:
        """The chart's unit from its first ref (a data item's own unit, or % for a percentage display). The
        scale is the drafter's display choice: code computes every point at that scale either way."""
        for ref in refs:
            base, _, name = ref.partition(":")
            if not name and base in self.verified and self.verified[base].value is not None:
                v = self.verified[base].value
                return ("%", "one") if v.unit == "%" else (v.unit, hint_scale)
            c = self.conclusions.get(base)
            comp = next((x for x in c.computations if x.name == name), None) if c else None
            if comp is not None:
                toks = scan_numbers(comp.display, include_labels=True)
                if toks and toks[0].pct:
                    return "%", "one"
        return hint_unit, hint_scale

    def chart(self, gi: int, group: list[str], dg: drafter.DraftChart) -> m.Chart | None:
        resolved = [(s.name, p.x, self._resolve_ref(gi, p.ref), p.ref) for s in dg.series for p in s.points]
        refs_ok = [r for _, _, r, _ in resolved if r]
        unit, scale = self._unit_scale(refs_ok, dg.unit.strip(), dg.scale)
        series: dict[str, list[m.ChartPoint]] = {}
        for sname, x, ref, raw in resolved:
            y = point_value(ref, unit, scale, {**self.verified, **self.conclusions}) if ref else None
            if y is None:
                self.note(gi, f"chart {dg.title!r}: point {x} ({raw}) dropped, it has no verified value in {unit}")
                continue
            series.setdefault(sname, []).append(m.ChartPoint(x=x, y=float(half_up(y, 1)), ref=ref))
        if not series:
            self.note(gi, f"chart {dg.title!r} dropped, no point could be computed")
            return None
        refs = list(dict.fromkeys(p.ref.split(":")[0] for pts in series.values() for p in pts))
        crit = dg.criterion if dg.criterion in group else group[0]
        return m.Chart(id=self.ctx.next_id("G"), category=RUBRIC["criteria"][crit]["category"], title=dg.title.strip(),
                       chart_type=dg.chart_type, unit=unit, scale=scale,
                       series=[m.ChartSeries(name=n, points=p) for n, p in series.items()], refs=refs,
                       verification=m.Verification(status="pending"))


_ID = re.compile(r"\b[A-Z]{1,4}-\d{1,4}\b")
_WORD = re.compile(r"\b[A-Za-z_]\w*\b")
_KEY = re.compile(r"[a-z]{1,3}\d{1,3}")     # drafter keys code swaps for ids in score texts: c1, c12, cn3


def inputs_of(c: m.Conclusion, name: str) -> list[str]:
    """Data ids a computation reads, directly or through the earlier computations it names."""
    comps = {x.name: x for x in c.computations}
    out: list[str] = []
    seen: set[str] = set()
    todo = [name]
    while todo:
        n = todo.pop()
        if n in seen or n not in comps:
            continue
        seen.add(n)
        expr = comps[n].expr
        out += [i for i in compute.ids_in(expr) if i not in out]
        todo += [w for w in _WORD.findall(_ID.sub(" ", expr)) if w in comps]
    return out


def _count(n: int) -> str:
    return _NUMBER_WORDS.get(n, str(n))


_CURRENCY_UNITS = {"EUR", "USD", "GBP", "CHF", "JPY"}
_CUR_MARK = re.compile(r"€|\$|£|¥|\b(?:EUR|USD|GBP|CHF|JPY)\b")


def display_currency(c: m.Conclusion, comp: m.Computation, verified: dict[str, m.DataItem]) -> tuple[str, str] | None:
    """(shown, expected) when a display names one currency and every amount it reads is in another. Seen live
    (B-fix-yield run 1): GE HealthCare's NOPAT and TICAP shown in € although every input was a USD row, and the
    judges rejected the claim for it."""
    toks = scan_numbers(vtext.normalize(comp.display), include_labels=True)
    if not toks or toks[0].pct or not toks[0].currency:
        return None
    units = {verified[i].value.unit for i in inputs_of(c, comp.name)
             if i in verified and verified[i].value is not None and verified[i].value.unit in _CURRENCY_UNITS}
    if len(units) != 1 or toks[0].currency in units:
        return None
    return toks[0].currency, units.pop()


def recurrency(display: str, cur: str) -> str:
    """The display with its currency marker swapped: "€2,441.1 million" in USD is "USD 2,441.1 million"."""
    mm = _CUR_MARK.search(display)
    if mm is None:
        return display
    mark = cur if mm.group(0).isalpha() else ("€" if cur == "EUR" else f"{cur} ")
    return re.sub(r"\s{2,}", " ", display[:mm.start()] + mark + display[mm.end():]).strip()


def fix_currencies(c: m.Conclusion, verified: dict[str, m.DataItem]) -> tuple[m.Conclusion, list[str]]:
    """Code writes each computed amount in the currency of the rows it reads, in the display and in the text.
    Only the marker changes, never the number, so every check sees the same value."""
    text, comps, notes = c.text, [], []
    for comp in c.computations:
        found = display_currency(c, comp, verified)
        if found is None:
            comps.append(comp)
            continue
        new = recurrency(comp.display, found[1])
        text = text.replace(comp.display, new)
        notes.append(f"{comp.name} display {comp.display} rewritten as {new} (its inputs are {found[1]} rows)")
        comps.append(comp.model_copy(update={"display": new}))
    if not notes:
        return c, []
    return c.model_copy(update={"text": text, "computations": comps}), notes


async def _no_draft() -> None:
    """A group with no verified data gets no drafter call."""
    return None


def _dec(x: float) -> Decimal:
    return Decimal(repr(x)) if isinstance(x, float) else Decimal(x)


def _missing_reason(group_has_data: bool, failure: str | None) -> str:
    if not group_has_data:
        return "No verified data item serves this criterion."
    if failure:
        return failure
    return "The drafter returned no score for this criterion."


def build_items(ctx: RunContext, scope: list[str], groups: list[list[str]], verified: dict[str, m.DataItem],
                results: list[tuple[drafter.DraftOutput | None, str] | None],
                shown: list[set[str]]) -> list[m.Item]:
    """Contract items from the drafts, in id order: conclusions, then one score per criterion, then charts.
    shown[gi] holds the data ids group gi's drafter saw: it may cite only those."""
    b = Builder(ctx, verified)
    b.agent_ids = {gi: r[1] for gi, r in enumerate(results) if r is not None and r[0] is not None}
    b.seen = dict(enumerate(shown))
    out: list[m.Item] = []
    for gi, (group, res) in enumerate(zip(groups, results)):
        if res is not None and res[0] is not None:
            for dc in res[0].conclusions:
                c = b.conclusion(gi, group, dc)
                if c is not None:
                    out.append(c)
    group_of = {c: gi for gi, g in enumerate(groups) for c in g}
    for crit in scope:
        gi = group_of[crit]
        res = results[gi]
        draft = res[0] if res is not None else None
        ds = next((s for s in draft.scores if s.criterion == crit), None) if draft is not None else None
        failure = res[1] if res is not None and res[0] is None else None
        out.append(b.extrapolation(crit, gi if draft is not None else None, ds, _missing_reason(res is not None, failure)))
    for gi, (group, res) in enumerate(zip(groups, results)):
        if res is not None and res[0] is not None:
            for dg in res[0].charts:
                g = b.chart(gi, group, dg)
                if g is not None:
                    out.append(g)
    for gi, lines in b.notes.items():
        aid = b.agent_ids.get(gi)
        ctx.emit("agent.log", m.AgentLogData(text="Code rejected or changed: " + "; ".join(lines),
                                             transcript_path=ctx.state.agents[aid].transcript_path if aid else None),
                 message=f"{aid or 'draft'}: code rejected or changed {len(lines)} part(s) of the draft",
                 agent_id=aid, level="warn")
    if b.info:
        ctx.emit("agent.log", m.AgentLogData(text="Code noted: " + "; ".join(b.info)),
                 message="draft: " + "; ".join(b.info))
    return out


async def run(ctx: RunContext) -> None:
    ctx.check_stop()
    state = ctx.state
    scope = in_scope(state.run.depth)
    groups = criterion_groups(scope, state.needs)
    data = usable_data(state)
    verified = {d.id: d for d in data}
    jobs, shown = [], []
    for group in groups:
        mine, left_out = group_items(data, group)
        rows = [data_row(d) for d in mine]
        index = inputs_index(state, mine) if metrics_for(group) else None
        if left_out:
            ctx.emit("agent.log", m.AgentLogData(text=f"Draft {', '.join(group)}: {left_out} verified data item(s) "
                                                      f"left out of the prompt (cap {MAX_ROWS} rows, lowest tier first)"),
                     message=f"draft: {left_out} data item(s) over the row cap for {', '.join(group)}", level="warn")
        shown.append({r["id"] for r in rows})
        jobs.append(_draft_group(ctx, group, rows, verified, index) if rows else _no_draft())
    results = list(await asyncio.gather(*jobs))
    ctx.check_stop()
    for item in build_items(ctx, scope, groups, verified, results, shown):
        state.items[item.id] = item
        ctx.emit("item.created", m.ItemCreatedData(item=item), message=f"{item.id} drafted ({item.kind})",
                 agent_id=getattr(item, "agent_id", None))
