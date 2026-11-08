// In-browser mock backend that follows docs/API.md and plays through a full run.
// Numbers and quotes here are made-up fixtures for the demo, not real company data.

import type {
  Api, Claim, CodeCheck, Report, RunDetail, RunEvent, Source, Step, Tier, Verdict,
} from "./types";

const STEP_DEFS: Omit<Step, "status">[] = [
  { n: 1, name: "Enter company", actor: "H", stage: 1 },
  { n: 2, name: "Decide data types", actor: "A", stage: 1 },
  { n: 3, name: "Propose sources", actor: "A", stage: 1 },
  { n: 4, name: "Rank & approve sources", actor: "A+H", stage: 1 },
  { n: 5, name: "Fetch sources", actor: "A+C", stage: 1 },
  { n: 6, name: "Build initial report", actor: "A", stage: 2 },
  { n: 7, name: "Verify data claims", actor: "C", stage: 2 },
  { n: 8, name: "Verify conclusions", actor: "A", stage: 2 },
  { n: 9, name: "Verify extrapolation", actor: "A", stage: 2 },
  { n: 10, name: "Build human report", actor: "A", stage: 2 },
  { n: 11, name: "Verify summary", actor: "A+C", stage: 3 },
  { n: 12, name: "Display report", actor: "C", stage: 3 },
];

const TIER1 = ["sec.gov", "deutsche-boerse.com", "bundesanzeiger.de", "europa.eu"];
const TIER2 = ["reuters.com", "bloomberg.com", "ft.com", "wsj.com", "fiercebiotech.com"];

const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

// Code assigns tiers from a domain list; the agent never does.
function tierFor(domain: string, companyDomain: string): Tier {
  if (domain === companyDomain || TIER1.some((d) => domain.endsWith(d))) return 1;
  if (TIER2.some((d) => domain.endsWith(d))) return 2;
  return 3;
}

function domainOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

interface State {
  run: RunDetail;
  events: RunEvent[];
  sources: Source[];
  claims: Claim[];
  report: Report | null;
  stopped: boolean;
  approve: (() => void) | null;
  nextSource: number;
}

const SPEED =
  typeof window === "undefined" ? 1 : Number(new URLSearchParams(window.location.search).get("speed") ?? "1") || 1;
const rand = (a: number, b: number) => Math.round(a + Math.random() * (b - a));
const hash = () =>
  Array.from({ length: 16 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
const clone = <T,>(x: T): T => structuredClone(x);

class StoppedError extends Error {}

// Extra data claims that back the generated charts (all mock).
const chartClaims: [string, string, string, string, number][] = [
  ["C13", "Q1 FY2025 comparable growth was 4.2%.", "4.2", "Q1 comparable revenue growth of 4.2%", 4100],
  ["C14", "Q2 FY2025 comparable growth was 4.8%.", "4.8", "Q2 comparable revenue growth of 4.8%", 4180],
  ["C15", "Q3 FY2025 comparable growth was 5.3%.", "5.3", "Q3 comparable revenue growth of 5.3%", 4260],
  ["C16", "Q4 FY2025 comparable growth was 5.9%.", "5.9", "Q4 comparable revenue growth of 5.9%", 4340],
  ["C17", "FY2024 adjusted EBIT margin was 15.8%.", "15.8", "prior-year margin of 15.8%", 2290],
  ["C18", "Imaging added 1.0 point to the margin.", "+1.0", "Imaging contributed 1.0 percentage point", 2700],
  ["C19", "Varian added 0.6 points to the margin.", "+0.6", "Varian contributed 0.6 percentage points", 2760],
  ["C20", "Diagnostics reduced the margin by 0.4 points.", "-0.4", "Diagnostics reduced margin by 0.4 percentage points", 2820],
  ["C21", "Other items added 0.2 points to the margin.", "+0.2", "other effects of 0.2 percentage points", 2880],
  ["C22", "Imaging was 48% of revenue.", "48", "Imaging accounted for 48% of revenue", 5400],
  ["C23", "Diagnostics was 19% of revenue.", "19", "Diagnostics accounted for 19% of revenue", 5460],
  ["C24", "Varian was 18% of revenue.", "18", "Varian accounted for 18% of revenue", 5520],
  ["C25", "Advanced Therapies was 9% of revenue.", "9", "Advanced Therapies accounted for 9% of revenue", 5580],
  ["C26", "Other businesses were 6% of revenue.", "6", "other businesses accounted for 6% of revenue", 5640],
];

export function mockApi(): Api {
  const runs = new Map<string, State>();
  let runCounter = 41;

  const get = (id: string) => {
    const s = runs.get(id);
    if (!s) throw new Error(`run ${id} not found`);
    return s;
  };

  function emit(
    s: State,
    e: Pick<RunEvent, "step" | "actor" | "type" | "summary"> & Partial<RunEvent>,
  ) {
    s.events.push({
      decision: null, source_id: null, claim_id: null, url: null, resolves: null, action: null, latency_ms: null, tokens: null,
      ...e,
      seq: s.events.length + 1,
      ts: new Date().toISOString(),
    });
  }

  const sleep = (s: State, ms: number) =>
    new Promise<void>((resolve, reject) => {
      setTimeout(() => (s.stopped ? reject(new StoppedError()) : resolve()), ms / SPEED);
    });

  function setStep(s: State, n: number, status: Step["status"]) {
    const step = s.run.steps[n - 1];
    step.status = status;
    if (status === "running" || status === "waiting") s.run.current_step = n;
    emit(s, {
      step: n, actor: { kind: "gateway", id: "pipeline" }, type: "step",
      summary: `Step ${n} · ${step.name}: ${status}`,
    });
  }

  function modelCall(s: State, step: number, id: string, summary: string, extra: Partial<RunEvent> = {}) {
    emit(s, {
      step, actor: { kind: "agent", id }, type: "model_call", decision: "allow",
      summary, latency_ms: rand(800, 4200), tokens: rand(1200, 18000), ...extra,
    });
  }

  function addSourceRecord(s: State, url: string, title: string, added_by: Source["added_by"]): Source {
    const domain = domainOf(url);
    const companyDomain = `${slugify(s.run.company)}.com`;
    const tier = tierFor(domain, companyDomain);
    const src: Source = {
      id: `S${s.nextSource++}`, title, url, domain, tier,
      priority: null,
      status: tier === 1 ? "proposed" : "approved",
      approval: tier === 1 ? null : "auto",
      added_by, chars: null, sha256: null,
    };
    s.sources.push(src);
    renumber(s);
    return src;
  }

  function renumber(s: State) {
    s.sources
      .filter((x) => x.tier === 1 && x.status !== "removed")
      .forEach((x, i) => (x.priority = i + 1));
    s.sources.filter((x) => x.tier !== 1 || x.status === "removed").forEach((x) => (x.priority = null));
  }

  function fixtureSources(company: string) {
    const d = `${slugify(company)}.com`;
    return [
      [`https://www.${d}/investor-relations/annual-report-2025`, `${company} Annual Report FY2025`],
      [`https://www.${d}/investor-relations/q4-2025-earnings-release`, `Q4 FY2025 earnings release`],
      [`https://www.${d}/investor-relations/q4-2025-call-transcript`, `Q4 FY2025 earnings call transcript`],
      [`https://www.${d}/investor-relations/capital-markets-day-2025`, `Capital Markets Day 2025 presentation`],
      [`https://www.deutsche-boerse.com/listing/${slugify(company)}`, `Exchange listing & share data`],
      [`https://www.reuters.com/business/healthcare/${slugify(company)}-q4-margins`, `Reuters: ${company} beats margin expectations`],
      [`https://www.ft.com/content/medtech-tariffs-imaging`, `FT: Tariffs weigh on medical imaging makers`],
      [`https://www.fiercebiotech.com/medtech/${slugify(company)}-oncology-segment`, `Fierce: Oncology segment growth`],
      [`https://www.reddit.com/r/stocks/comments/${slugify(company)}_diagnostics`, `Reddit r/stocks: "Diagnostics is falling apart"`],
      [`https://medium.com/@valueinvestor/${slugify(company)}-deep-dive`, `Medium: A value investor's deep dive`],
    ] as const;
  }

  function fixtureClaims(): Claim[] {
    const c = (
      id: string, kind: Claim["kind"], text: string, value: string | null,
      citations: [string, string, number][], scenario: Claim["scenario"] = null,
    ): Claim => ({
      id, kind, text, value, scenario,
      citations: citations.map(([source_id, quote, start]) => ({ source_id, quote, start, found_at: null })),
      code_check: { status: "pending", detail: "" },
      agent_check: { verdict: "pending", reason: "", validator: null },
      resolution: null,
    });
    return [
      c("C1", "data", "Revenue for FY2025 was €9,999 million.", "9,999", [["S1", "Revenue rose to €9,999 million", 18234]]),
      c("C2", "data", "Comparable revenue growth was 5.1% for FY2025.", "5.1%", [["S2", "comparable revenue growth of 5.1%", 1420]]),
      c("C3", "data", "Adjusted EBIT margin reached 17.2%.", "17.2%", [["S2", "adjusted EBIT margin of 17.2%", 2210]]),
      c("C4", "data", "Diagnostics revenue fell 3% year over year.", "3%", [["S9", "diagnostics down like 3% again", 311]]),
      c("C5", "data", "Free cash flow was €2.1 billion.", "€2.1bn", [["S1", "free cash flow of €2.1 billion", 40112]]),
      c("C6", "data", "Book-to-bill ratio stayed above 1.1.", ">1.1", [["S3", "book-to-bill above 1.1", 8890]]),
      c("C7", "conclusion", "Margin expansion was driven mainly by Imaging and the Oncology segment.", null,
        [["S2", "Imaging and Oncology were the main contributors to margin improvement", 2604], ["S8", "oncology unit posted its strongest quarter", 940]]),
      c("C8", "conclusion", "The Diagnostics turnaround is complete.", null,
        [["S3", "we are making progress on the Diagnostics transformation", 12020]]),
      c("C9", "conclusion", "US tariffs are a near-term headwind for imaging margins.", null,
        [["S7", "tariffs could shave up to one percentage point off imaging margins", 3310]]),
      c("C10", "extrapolation", "Bull case: comparable revenue growth of 7% in FY2026.", "7%",
        [["S4", "mid-term target of 6 to 8% comparable revenue growth", 5120]], "bull"),
      c("C11", "extrapolation", "Base case: comparable revenue growth of 5.5% in FY2026.", "5.5%",
        [["S2", "outlook of 5 to 6% comparable revenue growth for FY2026", 3105]], "base"),
      c("C12", "extrapolation", "Bear case: comparable revenue growth of 3% in FY2026 if tariffs escalate.", "3%",
        [["S7", "tariffs could shave up to one percentage point off imaging margins", 3310], ["S2", "outlook of 5 to 6%", 3105]], "bear"),
      ...chartClaims.map(([id, text, value, quote, start]) => c(id, "data", text, value, [["S2", quote, start]])),
    ];
  }

  async function play(s: State) {
    const company = s.run.company;
    try {
      // Step 1 already done by the human.
      setStep(s, 2, "running");
      await sleep(s, 1200);
      modelCall(s, 2, "planner", `Data types for ${company}: financials, segment results, guidance, market sentiment`);
      setStep(s, 2, "done");

      setStep(s, 3, "running");
      await sleep(s, 1500);
      modelCall(s, 3, "planner", "Proposed 10 sources");
      for (const [url, title] of fixtureSources(company)) {
        const src = addSourceRecord(s, url, title, "agent");
        emit(s, {
          step: 3, actor: { kind: "code", id: "tier-rules" }, type: "source_change", source_id: src.id,
          summary: `${src.id} ${src.domain} → tier ${src.tier}${src.tier === 1 ? " (needs human approval)" : " (auto-approved)"}`,
        });
        await sleep(s, 180);
      }
      setStep(s, 3, "done");

      // Agent tries to promote a low-trust source. Only a human may change tiers.
      await sleep(s, 600);
      emit(s, {
        step: 4, actor: { kind: "agent", id: "planner" }, type: "blocked", decision: "deny", source_id: "S10",
        summary: "Agent tried to raise S10 (medium.com) to tier 1. Only a human can change tiers.",
      });

      setStep(s, 4, "waiting");
      s.run.status = "awaiting_approval";
      await new Promise<void>((resolve) => (s.approve = resolve));
      s.approve = null;
      if (s.stopped) throw new StoppedError();
      s.run.status = "running";
      setStep(s, 4, "done");

      setStep(s, 5, "running");
      const toFetch = s.sources.filter((x) => x.status === "approved");
      for (const src of toFetch) {
        await sleep(s, rand(250, 600));
        src.status = "fetched";
        src.chars = rand(4000, 220000);
        src.sha256 = hash();
        emit(s, {
          step: 5, actor: { kind: "agent", id: "fetcher" }, type: "fetch", decision: "allow", source_id: src.id,
          summary: `GET ${src.domain} · ${src.chars.toLocaleString()} chars stored (sha256 ${src.sha256.slice(0, 8)}…)`,
          latency_ms: rand(120, 900),
        });
        if (src.id === "S3") {
          await sleep(s, 400);
          emit(s, {
            step: 5, actor: { kind: "agent", id: "fetcher" }, type: "blocked", decision: "deny",
            url: "https://stock-tips-daily.biz/hot-picks",
            summary: "GET stock-tips-daily.biz/hot-picks blocked: not an approved source (link found inside S3)",
          });
        }
      }
      setStep(s, 5, "done");

      setStep(s, 6, "running");
      await sleep(s, 2500);
      s.claims = fixtureClaims().filter((c) =>
        c.citations.every((ct) => s.sources.some((x) => x.id === ct.source_id && x.status === "fetched")),
      );
      modelCall(s, 6, "writer", `Initial report drafted: ${s.claims.length} claims with citations`);
      setStep(s, 6, "done");

      // Step 7: code checks every data claim against stored source text.
      setStep(s, 7, "running");
      const step7: Record<string, [CodeCheck, string, number | null]> = {
        C1: ["not_found", "Quote not found anywhere in S1's stored text, and 9,999 appears nowhere in S1. Treated as fabricated.", null],
        C2: ["pass", "Exact quote found at claimed position.", 1420],
        C3: ["position_off", "Quote found at 2,486, not 2,210. Passed with warning.", 2486],
        C4: ["tier_violation", "Only support is S9 (Reddit, tier 3). Numbers need a tier 1 source.", 311],
        C5: ["pass", "Exact quote found at claimed position.", 40112],
        C6: ["pass", "Exact quote found at claimed position.", 8890],
      };
      for (const claim of s.claims.filter((c) => c.kind === "data")) {
        await sleep(s, 500);
        const [status, detail, found] = step7[claim.id] ?? ["pass", "Exact quote found.", claim.citations[0].start];
        claim.code_check = { status, detail };
        claim.citations[0].found_at = found;
        const bad = status === "not_found" || status === "tier_violation" || status === "value_mismatch";
        emit(s, {
          step: 7, actor: { kind: "code", id: "quote-check" }, type: bad && status === "tier_violation" ? "blocked" : "check",
          decision: bad ? "deny" : "allow", claim_id: claim.id, source_id: claim.citations[0].source_id,
          summary: `${claim.id} "${claim.value}" → ${status.replace("_", " ")}`,
        });
      }
      for (const claim of s.claims.filter((c) => c.kind !== "data")) {
        claim.code_check = { status: "pass", detail: "All quotes found in stored text." };
        claim.citations.forEach((ct) => (ct.found_at = ct.start));
      }
      setStep(s, 7, "done");

      // Step 8: one validator per source, in parallel.
      setStep(s, 8, "running");
      const live = s.claims.filter((c) => !["not_found", "tier_violation", "source_removed"].includes(c.code_check.status));
      const bySource = new Map<string, Claim[]>();
      live.forEach((c) => c.citations.forEach((ct) => bySource.set(ct.source_id, [...(bySource.get(ct.source_id) ?? []), c])));
      const verdicts: Record<string, [Verdict, string]> = {
        C8: ["misleading", "Source says 'making progress', not 'complete'. Overstates the quote."],
      };
      await Promise.all(
        [...bySource.entries()].map(async ([sid, claims], i) => {
          await sleep(s, 300 + i * 150);
          emit(s, {
            step: 8, actor: { kind: "agent", id: `validator:${sid}` }, type: "model_call", decision: "allow", source_id: sid,
            summary: `Validator ${sid} started: ${claims.length} claim${claims.length > 1 ? "s" : ""}`,
            tokens: rand(3000, 12000),
          });
          await sleep(s, rand(2000, 5000));
          for (const c of claims) {
            const [verdict, reason] = verdicts[c.id] ?? ["supported", `Quote in ${sid} supports the claim in context.`];
            if (c.agent_check.verdict === "pending" || verdict !== "supported") {
              c.agent_check = { verdict, reason, validator: `validator:${sid}` };
            }
            emit(s, {
              step: 8, actor: { kind: "agent", id: `validator:${sid}` }, type: "check",
              decision: verdict === "supported" ? "allow" : "deny", claim_id: c.id, source_id: sid,
              summary: `${c.id}: ${verdict}`, latency_ms: rand(900, 3000),
            });
          }
        }),
      );
      emit(s, {
        step: 8, actor: { kind: "code", id: "cross-source" }, type: "check", decision: "allow",
        summary: "Cross-source check: no conflicting values for the same metric and period",
      });
      setStep(s, 8, "done");

      setStep(s, 9, "running");
      await sleep(s, 1500);
      emit(s, {
        step: 9, actor: { kind: "code", id: "scenario-order" }, type: "check", decision: "allow",
        summary: "bear 3% ≤ base 5.5% ≤ bull 7%: consistent",
      });
      modelCall(s, 9, "scenario-checker", "Each scenario's assumptions are tied to cited facts");
      setStep(s, 9, "done");

      setStep(s, 10, "running");
      await sleep(s, 2000);
      modelCall(s, 10, "writer", "Human-readable report written from verified claims only");
      s.report = buildReport(s);
      setStep(s, 10, "done");

      setStep(s, 11, "running");
      await sleep(s, 1200);
      emit(s, {
        step: 11, actor: { kind: "code", id: "summary-numbers" }, type: "check", decision: "allow",
        summary: "Every number in the summary exists in the verified report",
      });
      modelCall(s, 11, "summary-checker", "Wording drift fixed: 'margins surged' → 'margin reached 17.2%'");
      setStep(s, 11, "done");

      setStep(s, 12, "done");
      s.run.status = "completed";
    } catch (e) {
      if (!(e instanceof StoppedError)) {
        s.run.status = "failed";
        console.error(e);
      }
    }
  }

  function buildReport(s: State): Report {
    const ok = (id: string) => {
      const c = s.claims.find((x) => x.id === id);
      return (
        !!c && c.resolution?.action !== "drop" &&
        !["not_found", "tier_violation", "source_removed", "pending"].includes(c.code_check.status) && c.agent_check.verdict === "supported"
      );
    };
    const sentence = (id: string) => {
      const c = s.claims.find((x) => x.id === id);
      return c ? `${c.text.replace(/\.$/, "")} ${cite(id)}.` : "";
    };
    const cite = (id: string) =>
      (s.claims.find((x) => x.id === id)?.citations ?? []).map((ct) => `[${ct.source_id}]`).join("");
    const para = (text: string, ids: string[]) => ({ text, claim_ids: ids.filter(ok) });
    const keep = <T extends { claim_ids: string[] }>(ps: T[]) => ps.filter((p) => p.claim_ids.length > 0);
    return {
      title: `${s.run.company}: FY2025 review and FY2026 scenarios`,
      generated_at: new Date().toISOString(),
      sections: [
        {
          heading: "Results",
          paragraphs: keep([
            para(`Comparable revenue grew 5.1% in FY2025 ${cite("C2")}, and adjusted EBIT margin reached 17.2% ${cite("C3")}.`, ["C2", "C3"]),
            para(`Free cash flow was €2.1 billion ${cite("C5")}, with book-to-bill staying above 1.1 ${cite("C6")}.`, ["C5", "C6"]),
            para(sentence("C1"), ["C1"]),
          ]),
        },
        {
          heading: "Drivers and risks",
          paragraphs: keep([
            para(`Margin gains came mainly from Imaging and Oncology ${cite("C7")}.`, ["C7"]),
            para(`US tariffs are a near-term headwind for imaging margins ${cite("C9")}.`, ["C9"]),
            para(sentence("C4"), ["C4"]),
            para(sentence("C8"), ["C8"]),
          ]),
        },
        {
          heading: "FY2026 scenarios (not investment advice)",
          paragraphs: keep([
            para(`Bull: 7% comparable growth, at the middle of the mid-term target range ${cite("C10")}.`, ["C10"]),
            para(`Base: 5.5%, in line with company guidance ${cite("C11")}.`, ["C11"]),
            para(`Bear: 3% if tariffs escalate ${cite("C12")}.`, ["C12"]),
          ]),
        },
      ],
      charts: [
        {
          id: "F1", type: "line", title: "Comparable growth by quarter", unit: "%",
          caption: "Each point is a verified claim from quarterly releases.",
          points: [["Q1", "C13"], ["Q2", "C14"], ["Q3", "C15"], ["Q4", "C16"]].map(([label, claim_id]) => ({ label, claim_id })),
          lint: [{ check: "cited 4/4", ok: true }, { check: "zero baseline", ok: true }, { check: "one unit", ok: true }],
        },
        {
          id: "F2", type: "waterfall", title: "Adj. EBIT margin bridge", unit: "%",
          caption: "What moved the margin. Steps sum to the total.",
          points: [
            { label: "FY24", claim_id: "C17", role: "start" }, { label: "Imaging", claim_id: "C18", role: "delta" },
            { label: "Varian", claim_id: "C19", role: "delta" }, { label: "Diagn.", claim_id: "C20", role: "delta" },
            { label: "Other", claim_id: "C21", role: "delta" }, { label: "FY25", claim_id: "C3", role: "total" },
          ],
          lint: [{ check: "cited 6/6", ok: true }, { check: "sums match", ok: true }, { check: "axis starts at 14, marked", ok: false }],
        },
        {
          id: "F3", type: "fan", title: "FY2026 scenarios · comparable growth", unit: "%",
          caption: "Scenarios, not forecasts. Each end point names its assumption and source.",
          points: [
            { label: "FY25 actual", claim_id: "C2", role: "start" }, { label: "Bull", claim_id: "C10", role: "bull" },
            { label: "Base", claim_id: "C11", role: "base" }, { label: "Bear", claim_id: "C12", role: "bear" },
          ],
          lint: [{ check: "cited 4/4", ok: true }, { check: "bear ≤ base ≤ bull", ok: true }, { check: "zero baseline", ok: true }],
        },
        {
          id: "F4", type: "stacked_bar", title: "Revenue mix by segment", unit: "%",
          caption: "Share of FY2025 revenue by segment.",
          points: [["Imaging", "C22"], ["Diagnostics", "C23"], ["Varian", "C24"], ["Advanced Therapies", "C25"], ["Other", "C26"]]
            .map(([label, claim_id]) => ({ label, claim_id })),
          lint: [{ check: "cited 5/5", ok: true }, { check: "shares sum to 100", ok: true }],
        },
      ],
    };
  }

  return {
    async createRun(company) {
      const id = `run_${++runCounter}`;
      const s: State = {
        run: {
          id, company, status: "running", current_step: 1, started_by: "tong",
          created_at: new Date().toISOString(),
          steps: STEP_DEFS.map((d) => ({ ...d, status: "pending" })),
        },
        events: [], sources: [], claims: [], report: null, stopped: false, approve: null, nextSource: 1,
      };
      runs.set(id, s);
      setStep(s, 1, "done");
      emit(s, { step: 1, actor: { kind: "human", id: "tong" }, type: "approval", summary: `Run started for ${company}` });
      void play(s);
      return clone(s.run);
    },
    async listRuns() {
      return [...runs.values()].map((s) => {
        const { steps: _steps, ...run } = s.run;
        return clone(run);
      });
    },
    async getRun(id) {
      return clone(get(id).run);
    },
    async listEvents(id, after) {
      return clone(get(id).events.filter((e) => e.seq > after));
    },
    async stopRun(id, reason) {
      const s = get(id);
      if (s.stopped || s.run.status === "completed") return clone(s.run);
      s.stopped = true;
      s.run.status = "stopped";
      s.run.steps.forEach((st) => {
        if (st.status === "running" || st.status === "waiting") st.status = "stopped";
      });
      emit(s, {
        step: s.run.current_step, actor: { kind: "human", id: "tong" }, type: "stop", decision: "deny",
        summary: `Run stopped: ${reason}. All agent calls are now refused.`,
      });
      s.approve?.();
      return clone(s.run);
    },
    async listSources(id) {
      return clone(get(id).sources);
    },
    async addSource(id, url, title) {
      const s = get(id);
      const src = addSourceRecord(s, url, title || domainOf(url), "human");
      if (src.tier === 1) {
        src.status = "approved";
        src.approval = "human";
      }
      emit(s, {
        step: s.run.current_step, actor: { kind: "human", id: "tong" }, type: "source_change", source_id: src.id,
        summary: `Added ${src.id} ${src.domain} (tier ${src.tier})`,
      });
      return clone(src);
    },
    async setSourceTier(id, sid, tier) {
      const s = get(id);
      const src = s.sources.find((x) => x.id === sid);
      if (!src) throw new Error("source not found");
      src.tier = tier;
      if (tier === 1 && src.status !== "fetched") {
        src.status = "approved";
        src.approval = "human";
      }
      renumber(s);
      emit(s, {
        step: s.run.current_step, actor: { kind: "human", id: "tong" }, type: "source_change", source_id: sid,
        summary: `${sid} moved to tier ${tier} by a human`,
      });
      return clone(src);
    },
    async removeSource(id, sid) {
      const s = get(id);
      const src = s.sources.find((x) => x.id === sid);
      if (!src) throw new Error("source not found");
      src.status = "removed";
      renumber(s);
      const hit = s.claims.filter((c) => c.citations.some((ct) => ct.source_id === sid));
      hit.forEach((c) => (c.code_check = { status: "source_removed", detail: `${sid} was removed by a human; claim needs new support.` }));
      emit(s, {
        step: s.run.current_step, actor: { kind: "human", id: "tong" }, type: "source_change", source_id: sid,
        summary: `Removed ${sid} ${src.domain}${hit.length ? ` · ${hit.length} claim(s) now unsupported` : ""}`,
      });
      if (s.report) s.report = buildReport(s);
      return clone(src);
    },
    async reorderSources(id, ids) {
      const s = get(id);
      const t1 = ids.map((i) => s.sources.find((x) => x.id === i)).filter((x): x is Source => !!x);
      const rest = s.sources.filter((x) => !ids.includes(x.id));
      s.sources = [...t1, ...rest];
      renumber(s);
      emit(s, {
        step: s.run.current_step, actor: { kind: "human", id: "tong" }, type: "source_change",
        summary: `Tier 1 priority set: ${ids.join(" > ")}`,
      });
      return clone(s.sources);
    },
    async approveSources(id) {
      const s = get(id);
      s.sources.filter((x) => x.tier === 1 && x.status === "proposed").forEach((x) => {
        x.status = "approved";
        x.approval = "human";
      });
      emit(s, {
        step: 4, actor: { kind: "human", id: "tong" }, type: "approval", decision: "allow",
        summary: `Approved ${s.sources.filter((x) => x.tier === 1 && x.status === "approved").length} tier 1 sources`,
      });
      s.approve?.();
      return clone(s.run);
    },
    async listClaims(id) {
      return clone(get(id).claims);
    },
    async getReport(id) {
      return clone(get(id).report);
    },
    async resolveBlocked(id, seq, action, note) {
      const s = get(id);
      const ev = s.events.find((e) => e.seq === seq);
      if (!ev || ev.type !== "blocked") throw new Error(`event ${seq} is not a blocked action`);
      if (s.events.some((e) => e.resolves === seq)) throw new Error(`event ${seq} is already resolved`);
      let summary = `Kept blocked: ${ev.summary}`;
      if (action === "allow" && ev.source_id) {
        await this.setSourceTier(id, ev.source_id, 1);
        summary = `Allowed: ${ev.source_id} promoted to tier 1 by a person`;
      } else if (action === "add_source" && ev.url) {
        const src = await this.addSource(id, ev.url, domainOf(ev.url));
        summary = `Added ${src.id} ${src.domain} as a source (tier ${src.tier})`;
      }
      emit(s, {
        step: s.run.current_step, actor: { kind: "human", id: "tong" }, type: "resolution", decision: action === "dismiss" ? "deny" : "allow",
        resolves: seq, action, summary: note ? `${summary} · “${note}”` : summary,
      });
      return clone(s.events[s.events.length - 1]);
    },
    async actOnClaim(id, cid, action, { note, text }) {
      const s = get(id);
      const c = s.claims.find((x) => x.id === cid);
      if (!c) throw new Error(`claim ${cid} not found`);
      const at = new Date().toISOString();
      const said = note ? ` · “${note}”` : "";
      const human = { kind: "human" as const, id: "tong" };
      const step = s.run.current_step;
      const rebuild = () => {
        if (s.report) s.report = buildReport(s);
      };
      const recheck = (detail: string, reason: string) => {
        c.code_check = { status: "pass", detail };
        c.citations.forEach((ct) => (ct.found_at = ct.start));
        c.agent_check = { verdict: "supported", reason, validator: `validator:${c.citations[0]?.source_id ?? "?"}` };
        c.resolution = { ...c.resolution!, state: "done" };
        emit(s, { step, actor: { kind: "code", id: "quote-check" }, type: "check", decision: "allow", claim_id: cid, summary: `${cid} re-checked → quote found` });
        emit(s, { step, actor: { kind: "agent", id: c.agent_check.validator! }, type: "check", decision: "allow", claim_id: cid, summary: `${cid}: supported` });
        rebuild();
      };

      if (action === "drop") {
        c.resolution = { action, by: "tong", note, at, state: "done" };
        emit(s, { step, actor: human, type: "resolution", decision: "deny", claim_id: cid, action, summary: `Dropped ${cid} from the report${said}` });
        rebuild();
      } else if (action === "promote_source") {
        const sid = c.citations[0]?.source_id;
        if (sid) await this.setSourceTier(id, sid, 1);
        c.resolution = { action, by: "tong", note, at, state: "working" };
        emit(s, { step, actor: human, type: "resolution", decision: "allow", claim_id: cid, action, summary: `Promoted ${sid} to tier 1 so it can back ${cid}${said}` });
        setTimeout(() => recheck(`Quote found; ${sid} is now tier 1 (promoted by a person).`, "Quote supports the claim; source promoted by a person."), 900 / SPEED);
      } else if (action === "edit" && text) {
        c.text = text;
        c.agent_check = { verdict: "pending", reason: "", validator: null };
        c.resolution = { action, by: "tong", note, at, state: "working" };
        emit(s, { step, actor: human, type: "resolution", decision: "allow", claim_id: cid, action, summary: `Edited ${cid}: “${text}”${said}` });
        setTimeout(() => recheck("All quotes found in stored text.", "Edited wording matches the cited quote."), 1200 / SPEED);
      } else {
        // send_back: the agent looks for real support and resubmits; checks run again.
        c.resolution = { action, by: "tong", note, at, state: "working" };
        c.code_check = { status: "pending", detail: "Agent is looking for support…" };
        c.agent_check = { verdict: "pending", reason: "", validator: null };
        emit(s, { step, actor: human, type: "resolution", decision: "allow", claim_id: cid, action, summary: `Sent ${cid} back to the agent${said}` });
        setTimeout(() => {
          const fix = FIXES[cid];
          emit(s, {
            step, actor: { kind: "agent", id: "fixer" }, type: "model_call", decision: "allow", claim_id: cid,
            latency_ms: rand(900, 2400), tokens: rand(2000, 6000),
            summary: fix ? `${cid} rewritten from ${fix.cite[0]}: “${fix.text}”` : `${cid}: no support found`,
          });
          if (fix) {
            c.text = fix.text;
            c.value = fix.value;
            c.citations = [{ source_id: fix.cite[0], quote: fix.cite[1], start: fix.cite[2], found_at: null }];
            recheck("Exact quote found at the stated position.", `Rewritten claim matches ${fix.cite[0]}.`);
          } else {
            c.code_check = { status: "not_found", detail: "Agent found no support; the claim stays out of the report." };
            c.agent_check = { verdict: "unsupported", reason: "No supporting quote in any approved source.", validator: null };
            c.resolution = { ...c.resolution!, state: "done" };
            rebuild();
          }
        }, 1800 / SPEED);
      }
      return clone(c);
    },
  };
}

// What the mock agent produces when a failed claim is sent back (mock data).
const FIXES: Record<string, { text: string; value: string | null; cite: [string, string, number] }> = {
  C1: { text: "Revenue for FY2025 was €22.1 billion.", value: "€22.1bn", cite: ["S1", "revenue of €22.1 billion", 18240] },
  C4: { text: "Diagnostics revenue fell 1.5% year on year.", value: "-1.5%", cite: ["S2", "Diagnostics revenue declined 1.5%", 2950] },
  C8: { text: "The Diagnostics transformation is making progress.", value: null, cite: ["S3", "we are making progress on the Diagnostics transformation", 12020] },
};
