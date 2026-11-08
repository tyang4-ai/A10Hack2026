// Axelrod in the terminal. Two screens: the projects list (every run, grouped by
// company) and one run's desk (stage bar, then tabs for sources, traffic, agents,
// the final report and the gateway policy). Keyboard only; every action is one API call.
import { Box, Text, useApp, useInput, useStdout } from "ink";
import { useEffect, useMemo, useState } from "react";
import type { Citation, DocumentPage, Item, Run, Source, SourceDecision, Stage, SummarySentence } from "../../../contract/types.ts";
import { MODE_LABEL, STAGES, STAGE_LABEL, formatValue, usd } from "../../src/lib/format.ts";
import { API_BASE, ApiError, api } from "./api.ts";
import { useRun } from "./useRun.ts";

// A run's live data plus a way to refetch it now.
type RunData = ReturnType<typeof useRun>;

const ACCENT = "#14b8a6";
const BAND = { high: "green", medium: "yellow", low: "red", none: "gray" } as const;
const STATUS = {
  running: ["cyan", "Running"],
  awaiting_approval: [ACCENT, "Waiting for your approval"],
  done: ["green", "Done"],
  stopped: ["red", "Stopped"],
  failed: ["red", "Failed"],
} as const;

const trunc = (s: string, n: number) => (n <= 1 ? "" : s.length > n ? s.slice(0, n - 1) + "…" : s);
const pad = (s: string, n: number) => trunc(s, n).padEnd(n);
const errText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));
const itemText = (i: Item) => (i.kind === "data" ? i.statement : i.kind === "chart" ? i.title : i.text);

function ago(iso: string) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

// Terminal size, kept current on resize.
function useSize() {
  const { stdout } = useStdout();
  const [size, setSize] = useState({ cols: stdout.columns || 100, rows: stdout.rows || 30 });
  useEffect(() => {
    const on = () => setSize({ cols: stdout.columns || 100, rows: stdout.rows || 30 });
    stdout.on("resize", on);
    return () => void stdout.off("resize", on);
  }, [stdout]);
  return size;
}

// Keep the selected row on screen in a list taller than the space for it.
function windowOf<T>(list: T[], selected: number, height: number) {
  const start = Math.max(0, Math.min(selected - Math.floor(height / 2), list.length - height));
  return { start, rows: list.slice(start, start + height) };
}

function Keys({ children }: { children: string }) {
  return <Text color="gray">{children}</Text>;
}

export function App({ initialRun }: { initialRun?: string }) {
  const [runId, setRunId] = useState<string | null>(initialRun ?? null);
  return runId ? <RunScreen runId={runId} onBack={() => setRunId(null)} /> : <Home onOpen={setRunId} />;
}

// ---------------------------------------------------------------------------
// Projects: every run, grouped by company
// ---------------------------------------------------------------------------

function Home({ onOpen }: { onOpen: (id: string) => void }) {
  const { exit } = useApp();
  const { cols, rows } = useSize();
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sel, setSel] = useState(0);
  const [naming, setNaming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api.listRuns().then(
        (r) => alive && (setRuns(r.runs), setErr(null)),
        (e) => alive && setErr(errText(e)),
      );
    void load();
    const t = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // Newest first, grouped by company. Each company is a project.
  const list = useMemo(() => {
    const sorted = [...(runs ?? [])].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    const by = new Map<string, Run[]>();
    for (const r of sorted) by.set(r.company, [...(by.get(r.company) ?? []), r]);
    return [...by.values()].flat();
  }, [runs]);

  useInput(
    (input, key) => {
      if (naming !== null) {
        if (key.escape) setNaming(null);
        else if (key.return && naming.trim() && !busy) {
          setBusy(true);
          api.createRun(naming.trim()).then(
            (run) => onOpen(run.run_id),
            (e) => (setErr(errText(e)), setBusy(false)),
          );
        } else if (key.backspace || key.delete) setNaming(naming.slice(0, -1));
        else if (input && !key.ctrl && !key.meta) setNaming(naming + input);
        return;
      }
      if (input === "q") exit();
      else if (input === "n") setNaming("");
      else if (key.upArrow) setSel((s) => Math.max(0, s - 1));
      else if (key.downArrow) setSel((s) => Math.min(list.length - 1, s + 1));
      else if (key.return && list[sel]) onOpen(list[sel].run_id);
    },
    { isActive: true },
  );

  const height = Math.max(3, rows - 9);
  // Columns drop out from the right as the terminal narrows; the run, status and stage stay longest.
  const show = { stage: cols >= 66, items: cols >= 78, blocked: cols >= 90, cost: cols >= 100, ago: cols >= 112 };
  const { start, rows: shown } = windowOf(list, sel, height);

  return (
    <Box flexDirection="column" paddingX={1}>
      <Box justifyContent="space-between">
        <Text>
          <Text bold>Axelrod</Text>
          <Text color="gray">  Validated Financial Market reports</Text>
        </Text>
        <Text color="gray">{API_BASE}</Text>
      </Box>
      <Box marginTop={1}>
        {naming !== null ? (
          <Text>
            <Text color={ACCENT}>Which company should we look into? </Text>
            {naming}
            <Text inverse> </Text>
            {busy && <Text color="gray">  starting…</Text>}
          </Text>
        ) : (
          <Text bold>Projects</Text>
        )}
      </Box>
      {err && <Text color="red">{err}</Text>}
      {!runs && !err && <Text color="gray">Loading runs…</Text>}
      {runs && list.length === 0 && <Text color="gray">No projects yet. Press n to start one.</Text>}
      <Box flexDirection="column" marginTop={1}>
        {shown.map((r, i) => {
          const at = start + i;
          const prev = list[at - 1];
          const [color, word] = STATUS[r.status];
          return (
            <Box key={r.run_id} flexDirection="column">
              {(!prev || prev.company !== r.company) && at > start - 1 && (
                <Text bold color="white">
                  {r.company}
                  {r.ticker ? <Text color="gray"> {r.ticker}</Text> : null}
                </Text>
              )}
              <Text inverse={at === sel} wrap="truncate-end">
                {at === sel ? "▸ " : "  "}
                {pad(r.run_id, 22)} {pad(r.mode === "live" ? "" : MODE_LABEL, 10)}
                <Text color={color}>{pad(cols < 100 && r.status === "awaiting_approval" ? "Needs approval" : word, cols < 100 ? 16 : 26)}</Text>
                {show.stage && pad(STAGE_LABEL[r.stage], 12)}
                {show.items && pad(`${r.counts.items_pass + r.counts.items_fixed} of ${r.counts.items_total}`, 10)}
                {show.blocked && <Text color={r.counts.gateway_blocked ? "red" : undefined}>{pad(`${r.counts.gateway_blocked} blocked`, 11)}</Text>}
                {show.cost && pad(usd(r.usage.cost_usd), 9)}
                {show.ago && <Text color="gray">{ago(r.updated_at)}</Text>}
              </Text>
            </Box>
          );
        })}
      </Box>
      <Box marginTop={1}>
        <Keys>{naming !== null ? "type a company  ⏎ start  esc cancel" : "↑↓ choose  ⏎ open  n new run  q quit"}</Keys>
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// One run's desk
// ---------------------------------------------------------------------------

type Tab = "sources" | "traffic" | "agents" | "report" | "policy";
const TABS: [Tab, string][] = [
  ["sources", "Sources"],
  ["traffic", "Traffic"],
  ["agents", "Agents"],
  ["report", "Final report"],
  ["policy", "Policy"],
];
type Decision = "approved" | "rejected" | null;

// The tab that shows what each step of the pipeline is doing.
function tabFor(stage: Stage): Tab {
  if (stage === "plan" || stage === "approve") return "sources";
  if (stage === "collect" || stage === "extract" || stage === "draft") return "traffic";
  if (stage === "verify" || stage === "repair") return "agents";
  return "report";
}

// Same rule as the dashboard: tier 2 and 3 and user-added sources default to approved.
function decisionOf(s: Source, drafts: Record<string, Decision>, awaiting: boolean): Decision {
  if (!awaiting) return s.approval === "rejected" ? "rejected" : s.approval === "pending" ? null : "approved";
  if (drafts[s.source_id] !== undefined) return drafts[s.source_id];
  if (s.tier !== "priority" || s.proposed_by === "user") return "approved";
  return null;
}

function RunScreen({ runId, onBack }: { runId: string; onBack: () => void }) {
  const { exit } = useApp();
  const { cols, rows } = useSize();
  const data = useRun(runId);
  const { run } = data;
  const [tab, setTab] = useState<Tab | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Decision>>({});
  const [stopArmed, setStopArmed] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [captured, setCaptured] = useState(false); // a pane is taking keys (detail view)

  // Follow the run until the analyst picks a tab.
  const current: Tab = tab ?? (run ? tabFor(run.stage) : "sources");
  const live = run && (run.status === "running" || run.status === "awaiting_approval");

  useEffect(() => {
    if (!stopArmed) return;
    const t = setTimeout(() => setStopArmed(false), 4000);
    return () => clearTimeout(t);
  }, [stopArmed]);

  useInput(
    (input, key) => {
      if (input === "q") return exit();
      if (key.escape) return onBack();
      const n = Number(input);
      if (n >= 1 && n <= TABS.length) return setTab(TABS[n - 1][0]);
      if (key.tab || key.rightArrow) return setTab(TABS[(TABS.findIndex(([t]) => t === current) + 1) % TABS.length][0]);
      if (key.leftArrow) return setTab(TABS[(TABS.findIndex(([t]) => t === current) + TABS.length - 1) % TABS.length][0]);
      if (input === "s" && live) {
        if (!stopArmed) return setStopArmed(true);
        setStopArmed(false);
        api.stop(runId, "Stopped by the analyst from the terminal").then(
          () => (setNote("Run stopped."), data.refresh()),
          (e) => setNote(errText(e)),
        );
      }
    },
    { isActive: !captured },
  );

  // The header stacks onto two lines below 110 columns, so the body gets one line less there.
  const bodyH = Math.max(5, rows - (cols >= 110 ? 8 : 9));
  if (!run) return <Box paddingX={1}>{data.error ? <Text color="red">{data.error}</Text> : <Text color="gray">Loading {runId}…</Text>}</Box>;

  const [sc, sw] = STATUS[run.status];
  const stageIdx = STAGES.indexOf(run.stage);
  const blocked = data.traffic.filter((t) => t.data.decision === "blocked").length;
  return (
    <Box flexDirection="column" paddingX={1} height={rows}>
      {/* Header: the run, its status and the meters */}
      {/* Below 110 columns the meters go on their own line under the run's name. */}
      <Box justifyContent="space-between" flexDirection={cols >= 110 ? "row" : "column"}>
        <Text wrap="truncate-end">
          <Text bold>Axelrod</Text>  <Text bold color="white">{run.company}</Text> <Text color="gray">{run.run_id.split("_").pop()}</Text>
          {run.mode !== "live" && <Text color="black" backgroundColor="yellow"> {MODE_LABEL} </Text>}
          {run.status !== "done" && <Text color={sc}>  ● {sw}</Text>}
        </Text>
        <Text color="gray" wrap="truncate-end">
          Cost <Text color="white">{usd(run.usage.cost_usd)}</Text>
          {run.usage.budget_usd !== null ? ` of ${usd(run.usage.budget_usd)}` : ""}  Items <Text color="white">{run.counts.items_total}</Text>  Agents{" "}
          <Text color="white">
            {run.counts.agents_active}/{run.counts.agents_total}
          </Text>{"  "}Blocked <Text color={blocked ? "red" : "white"}>{blocked}</Text>
        </Text>
      </Box>
      {/* Stage bar */}
      <Text wrap="truncate-end">
        {STAGES.map((s, i) => {
          const done = i < stageIdx || run.stage === "complete";
          const now = i === stageIdx && run.stage !== "complete";
          const halted = now && (run.status === "stopped" || run.status === "failed");
          const color = halted ? "red" : now ? ACCENT : done ? "green" : "gray";
          return (
            <Text key={s} color={color} bold={now}>
              {done ? "✓" : now ? "▶" : "·"}
              {/* Narrow terminals name only the current step. */}
              {cols >= 110 || now ? ` ${STAGE_LABEL[s]}` : ""}
              {i < STAGES.length - 1 ? (cols >= 110 ? "  " : " ") : ""}
            </Text>
          );
        })}
      </Text>
      {run.status === "stopped" && (
        <Text color="white" backgroundColor="red">
          {" "}Run stopped. Every agent request is blocked.{" "}
        </Text>
      )}
      {run.status === "failed" && <Text color="red">Run failed. {run.error ?? "No error details were returned."}</Text>}
      {/* Tabs */}
      <Box marginTop={1}>
        {TABS.map(([t, label], i) => (
          <Text key={t} inverse={t === current} color={t === current ? ACCENT : undefined}>
            {" "}
            {i + 1} {label}
            {t === "traffic" && blocked ? <Text color="red"> {blocked}</Text> : null}{" "}
          </Text>
        ))}
      </Box>
      <Box flexDirection="column" height={bodyH} marginTop={1}>
        {current === "sources" && <SourcesPane data={data} drafts={drafts} setDrafts={setDrafts} height={bodyH} width={cols - 2} setNote={setNote} />}
        {current === "traffic" && <TrafficPane data={data} height={bodyH} width={cols - 2} />}
        {current === "agents" && <AgentsPane data={data} width={cols - 2} />}
        {current === "report" && <ReportPane data={data} height={bodyH} width={cols - 2} setCaptured={setCaptured} />}
        {current === "policy" && <PolicyPane data={data} drafts={drafts} width={cols - 2} />}
      </Box>
      <Box justifyContent="space-between">
        <Keys>{`1-5 or ←→ tabs  ${live ? (stopArmed ? "s again to STOP the run  " : "s stop  ") : ""}esc projects  q quit`}</Keys>
        {note ? <Text color="gray">{note}</Text> : data.error ? <Text color="red">{data.error}</Text> : null}
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Sources: decide the priority sources at the approval gate
// ---------------------------------------------------------------------------

function SourcesPane({
  data,
  drafts,
  setDrafts,
  height,
  width,
  setNote,
}: {
  data: RunData;
  drafts: Record<string, Decision>;
  setDrafts: (f: (d: Record<string, Decision>) => Record<string, Decision>) => void;
  height: number;
  width: number;
  setNote: (s: string) => void;
}) {
  const run = data.run!;
  const awaiting = run.status === "awaiting_approval";
  const sources = useMemo(
    () => [...(data.plan?.sources ?? [])].sort((a, b) => a.tier.localeCompare(b.tier) || a.rank - b.rank),
    [data.plan],
  );
  const [sel, setSel] = useState(0);
  const [busy, setBusy] = useState(false);
  const priority = sources.filter((s) => s.tier === "priority");
  const undecided = priority.filter((s) => decisionOf(s, drafts, awaiting) === null);

  useInput((input, key) => {
    const s = sources[sel];
    if (key.upArrow) setSel((i) => Math.max(0, i - 1));
    else if (key.downArrow) setSel((i) => Math.min(sources.length - 1, i + 1));
    else if (!awaiting || !s) return;
    else if (input === "a") setDrafts((d) => ({ ...d, [s.source_id]: "approved" }));
    else if (input === "r") setDrafts((d) => ({ ...d, [s.source_id]: "rejected" }));
    else if (input === "A") setDrafts((d) => ({ ...d, ...Object.fromEntries(undecided.map((u) => [u.source_id, "approved" as const])) }));
    else if (key.return && !undecided.length && !busy) {
      setBusy(true);
      const req: SourceDecision[] = [
        ...priority.map((p, i) => ({ source_id: p.source_id, approval: decisionOf(p, drafts, true) as "approved" | "rejected", tier: "priority" as const, rank: i + 1 })),
        ...sources.filter((x) => x.tier !== "priority" && decisionOf(x, drafts, true) === "rejected").map((x) => ({ source_id: x.source_id, approval: "rejected" as const })),
      ];
      api.approve(run.run_id, { decisions: req }).then(
        () => (setNote("Approved. The run continues."), setBusy(false), data.refresh()),
        (e) => (setNote(errText(e)), setBusy(false)),
      );
    }
  });

  if (!sources.length) return <Text color="gray">Planning sources…</Text>;
  const { start, rows } = windowOf(sources, sel, height - 2);
  const domainW = width < 110 ? 18 : 30;
  const titleW = Math.max(14, width - 30 - domainW);
  return (
    <Box flexDirection="column">
      <Text bold>{awaiting ? "Approve sources" : "Sources"}</Text>
      {rows.map((s, i) => {
        const at = start + i;
        const d = decisionOf(s, drafts, awaiting);
        const tag = awaiting ? (d === "approved" ? ["green", "APPROVE"] : d === "rejected" ? ["red", "REJECT"] : ["yellow", "DECIDE"]) : [s.status === "blocked" || s.status === "revoked" || s.status === "failed" ? "red" : "green", s.status.toUpperCase()];
        return (
          <Text key={s.source_id} inverse={at === sel} wrap="truncate-end">
            {at === sel ? "▸ " : "  "}
            {pad(s.tier === "priority" ? `${s.rank}` : s.tier === "tier_2" ? "T2" : "T3", 3)}
            {pad(s.title, titleW)} <Text color="gray">{pad(s.source_id, 6)}</Text> {pad(s.domain, domainW)} <Text color={tag[0]}>{pad(tag[1], 10)}</Text>
          </Text>
        );
      })}
      <Box marginTop={1}>
        <Keys>
          {awaiting
            ? `↑↓ choose  a approve  r reject  A approve the rest  ${undecided.length ? `${undecided.length} source${undecided.length > 1 ? "s" : ""} still need${undecided.length > 1 ? "" : "s"} a decision` : "⏎ approve and run"}`
            : "↑↓ choose"}
        </Keys>
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Traffic: every agent request through the gateway, newest first
// ---------------------------------------------------------------------------

function TrafficPane({ data, height, width }: { data: RunData; height: number; width: number }) {
  const [onlyBad, setOnlyBad] = useState(false);
  useInput((input) => input === "b" && setOnlyBad((v) => !v));
  const list = (onlyBad ? data.traffic.filter((t) => t.data.decision !== "allowed") : data.traffic).slice(-(height - 2)).reverse();
  const narrow = width < 110;
  const targetW = Math.max(12, width - (narrow ? 54 : 70));
  return (
    <Box flexDirection="column">
      <Text color="gray">
        {pad("Time", 10)}
        {pad("Agent", 18)}
        {pad("Action", 14)}
        {pad("Decision", 10)}
        {narrow ? "" : pad("Rule", 16)}Target
      </Text>
      {list.map((e) => {
        const d = e.data;
        const color = d.decision === "blocked" ? "red" : d.decision === "flagged" ? "yellow" : undefined;
        return (
          <Text key={e.seq} color={color} wrap="truncate-end">
            {pad(new Date(e.ts).toLocaleTimeString("en-GB", { hour12: false }), 10)}
            {pad(e.agent_id ?? "—", 18)}
            {pad(d.action, 14)}
            <Text bold={!!color}>{pad(d.decision.toUpperCase(), 10)}</Text>
            {narrow ? "" : pad(d.rule_id ?? (/kill|stop/i.test(d.reason) ? "stopped" : "default deny"), 16)}
            {trunc(d.target, targetW)}
          </Text>
        );
      })}
      {list.length === 0 && <Text color="gray">No requests yet.</Text>}
      <Box marginTop={1}>
        <Keys>{`b ${onlyBad ? "show all requests" : "show only blocked and flagged"}`}</Keys>
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Agents: who is working, by role
// ---------------------------------------------------------------------------

const AGENT_COLOR = { queued: "gray", working: "cyan", done: "green", failed: "red", stopped: "yellow" } as const;

function AgentsPane({ data, width }: { data: RunData; width: number }) {
  const byRole = new Map<string, typeof data.agents>();
  for (const a of data.agents) byRole.set(a.role, [...(byRole.get(a.role) ?? []), a]);
  const working = data.agents.filter((a) => a.status === "working");
  return (
    <Box flexDirection="column">
      {[...byRole].map(([role, list]) => (
        <Text key={role}>
          {pad(role, 12)}
          <Text color="gray">{pad(String(list.length), 4)}</Text>
          {list.map((a) => (
            <Text key={a.agent_id} color={AGENT_COLOR[a.status]}>
              ●
            </Text>
          ))}
        </Text>
      ))}
      {byRole.size === 0 && <Text color="gray">No agents yet.</Text>}
      <Text color="gray">
        {Object.entries(AGENT_COLOR)
          .map(([k]) => k)
          .join("  ")}
      </Text>
      {working.length > 0 && (
        <Box flexDirection="column" marginTop={1}>
          <Text bold>Working now</Text>
          {working.map((a) => (
            <Text key={a.agent_id}>
              <Text color="cyan">{pad(a.agent_id, 20)}</Text>
              {trunc(a.task, width - 22)}
            </Text>
          ))}
        </Box>
      )}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Final report: sentences with certainty, then the chain down to the source quote
// ---------------------------------------------------------------------------

function sentencesOf(data: RunData): SummarySentence[] {
  return (data.summary?.sections ?? []).flatMap((sec) =>
    sec.blocks.flatMap((b) => (b.type === "paragraph" || b.type === "bullets" ? b.sentences : b.type === "chart" ? [b.caption] : [])),
  );
}

function citesOf(it: Item): string[] {
  if (it.kind === "data") return [it.source_id];
  if (it.kind === "conclusion") return it.data_ids;
  if (it.kind === "extrapolation") return it.conclusion_ids;
  return it.refs;
}

// Walk a sentence's sources down to data items, depth first, for the chain view.
function chainOf(refs: string[], items: Record<string, Item>, depth = 0): { depth: number; item: Item }[] {
  if (depth > 3) return [];
  return refs.flatMap((r) => {
    const it = items[r.split(":")[0]];
    if (!it) return [];
    return [{ depth, item: it }, ...(it.kind === "data" ? [] : chainOf(citesOf(it).slice(0, 8), items, depth + 1))];
  });
}

function ReportPane({ data, height, width, setCaptured }: { data: RunData; height: number; width: number; setCaptured: (v: boolean) => void }) {
  const sentences = useMemo(() => sentencesOf(data), [data.summary]);
  const [sel, setSel] = useState(0);
  const [open, setOpen] = useState<SummarySentence | null>(null);
  const [page, setPage] = useState<{ citation: Citation; itemId: string } | null>(null);
  useEffect(() => setCaptured(!!open), [open, setCaptured]);
  useEffect(() => () => setCaptured(false), [setCaptured]);

  useInput(
    (_input, key) => {
      if (key.upArrow) setSel((i) => Math.max(0, i - 1));
      else if (key.downArrow) setSel((i) => Math.min(sentences.length - 1, i + 1));
      else if (key.return && sentences[sel]) setOpen(sentences[sel]);
    },
    { isActive: !open },
  );

  if (!data.summary || data.summary.status === "not_started") return <Text color="gray">Written after the agent report is verified.</Text>;
  if (open && page) return <PageView runId={data.run!.run_id} citation={page.citation} itemId={page.itemId} height={height} onBack={() => setPage(null)} />;
  if (open) return <ChainView s={open} data={data} height={height} width={width} onBack={() => setOpen(null)} onPage={(citation, itemId) => setPage({ citation, itemId })} />;

  const { start, rows } = windowOf(sentences, sel, height - 3);
  return (
    <Box flexDirection="column">
      <Text wrap="truncate-end">
        <Text bold>{data.summary.title}</Text>
        <Text color="gray">
          {"  "}
          {data.summary.stats.words} words, certainty{" "}
        </Text>
        <Text color="white">{data.summary.certainty?.mean ?? "—"}</Text>
        <Text color="gray">, {data.summary.certainty?.high_pct ?? 0}% in the high band</Text>
      </Text>
      {rows.map((s, i) => {
        const at = start + i;
        return (
          <Text key={s.id} inverse={at === sel} wrap="truncate-end">
            {at === sel ? "▸ " : "  "}
            <Text color={BAND[s.certainty?.band ?? "none"]}>{pad(String(s.certainty?.score ?? "—"), 4)}</Text>
            <Text color="gray">{pad(s.id, 7)}</Text>
            {trunc(s.text, width - 15)}
          </Text>
        );
      })}
      <Box marginTop={1}>
        <Keys>↑↓ choose a sentence  ⏎ show the chain to its source</Keys>
      </Box>
    </Box>
  );
}

function ChainView({
  s,
  data,
  height,
  width,
  onBack,
  onPage,
}: {
  s: SummarySentence;
  data: RunData;
  height: number;
  width: number;
  onBack: () => void;
  onPage: (c: Citation, itemId: string) => void;
}) {
  const chain = useMemo(() => chainOf(s.derived_from, data.items), [s, data.items]);
  // Only data items with a quote on a stored text page can open a source page.
  const pickable = chain.filter((c) => c.item.kind === "data" && c.item.citations.some((x) => x.locator.type === "text"));
  const [sel, setSel] = useState(0);
  useInput((_i, key) => {
    if (key.escape) onBack();
    else if (key.upArrow) setSel((i) => Math.max(0, i - 1));
    else if (key.downArrow) setSel((i) => Math.min(pickable.length - 1, i + 1));
    else if (key.return && pickable[sel]) {
      const it = pickable[sel].item;
      if (it.kind === "data") onPage(it.citations.find((x) => x.locator.type === "text")!, it.id);
    }
  });
  const c = s.certainty;
  const lines = chain.slice(0, Math.max(3, height - 7));
  return (
    <Box flexDirection="column">
      <Text>
        <Text color="gray">{s.id}  </Text>
        <Text color={BAND[c?.band ?? "none"]}>certainty {c?.score ?? "—"} ({c?.band ?? "none"})</Text>
      </Text>
      <Text wrap="wrap">{s.text}</Text>
      {c && (
        <Text color="gray">
          support {c.support.toFixed(2)} × inherited {c.inherited.toFixed(2)} × completeness {c.completeness.toFixed(2)}. {c.basis}
        </Text>
      )}
      <Box flexDirection="column" marginTop={1}>
        {lines.map(({ depth, item }, i) => {
          const isPick = pickable[sel]?.item.id === item.id;
          const quote = item.kind === "data" ? item.citations[0]?.quote : null;
          return (
            <Box key={`${item.id}-${i}`} flexDirection="column">
              <Text inverse={isPick}>
                {"  ".repeat(depth)}
                {isPick ? "▸ " : "  "}
                <Text color={BAND[item.certainty?.band ?? "none"]}>{item.id}</Text> {trunc(itemText(item), width - 12 - depth * 2)}
              </Text>
              {quote && (
                <Text color="gray">
                  {"  ".repeat(depth + 2)}“{trunc(quote, width - 8 - depth * 2)}”{item.kind === "data" && item.value ? `  ${formatValue(item.value)}` : ""}
                </Text>
              )}
            </Box>
          );
        })}
        {chain.length === 0 && <Text color="gray">{s.id} cites the scorecard only. There's no source page to show.</Text>}
      </Box>
      <Box marginTop={1}>
        <Keys>{pickable.length ? "↑↓ choose a quote  ⏎ show its source page  esc back" : "esc back"}</Keys>
      </Box>
    </Box>
  );
}

// The stored source page with the quote highlighted, scrolled to it.
function PageView({ runId, citation, itemId, height, onBack }: { runId: string; citation: Citation; itemId: string; height: number; onBack: () => void }) {
  const loc = citation.locator.type === "text" ? citation.locator : null;
  const [doc, setDoc] = useState<DocumentPage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [scroll, setScroll] = useState<number | null>(null);
  useEffect(() => {
    if (loc) api.page(runId, citation.doc_id, loc.page).then(setDoc, (e) => setErr(errText(e)));
  }, [runId, citation.doc_id, loc?.page]);
  useInput((_i, key) => {
    if (key.escape) onBack();
    else if (key.upArrow) setScroll((s) => Math.max(0, (s ?? 0) - 1));
    else if (key.downArrow) setScroll((s) => (s ?? 0) + 1);
  });
  if (err) return <Text color="red">{err}</Text>;
  if (!doc || !loc) return <Text color="gray">Loading {citation.doc_id}…</Text>;

  // Split the page into lines, marking the cited span.
  const mine = doc.highlights.find((h) => h.item_id === itemId && h.role === "evidence") ?? { char_start: loc.char_start, char_end: loc.char_end };
  const lines: { parts: { t: string; hl: boolean }[] }[] = [];
  let at = 0;
  let hitLine = 0;
  for (const raw of doc.text.split("\n")) {
    const a = at;
    const b = at + raw.length;
    const parts: { t: string; hl: boolean }[] = [];
    const s0 = Math.max(a, mine.char_start);
    const e0 = Math.min(b, mine.char_end);
    if (s0 < e0) {
      if (s0 > a) parts.push({ t: raw.slice(0, s0 - a), hl: false });
      parts.push({ t: raw.slice(s0 - a, e0 - a), hl: true });
      if (e0 < b) parts.push({ t: raw.slice(e0 - a), hl: false });
      if (!hitLine) hitLine = lines.length;
    } else parts.push({ t: raw, hl: false });
    lines.push({ parts });
    at = b + 1;
  }
  const view = Math.max(3, height - 4);
  const top = scroll ?? Math.max(0, hitLine - Math.floor(view / 2));
  return (
    <Box flexDirection="column">
      <Text>
        <Text bold>{citation.doc_id}</Text>
        <Text color="gray">
          {"  "}page {loc.page} of {doc.pages}  stored source text
        </Text>
      </Text>
      {lines.slice(top, top + view).map((l, i) => (
        <Text key={top + i}>
          {l.parts.map((p, k) =>
            p.hl ? (
              <Text key={k} color="black" backgroundColor="yellow">
                {p.t}
              </Text>
            ) : (
              <Text key={k}>{p.t}</Text>
            ),
          )}
        </Text>
      ))}
      <Keys>{`${itemId} evidence highlighted  ↑↓ scroll  esc back`}</Keys>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Policy: the gateway's allowlist, with a preview while the analyst decides
// ---------------------------------------------------------------------------

function PolicyPane({ data, drafts, width }: { data: RunData; drafts: Record<string, Decision>; width: number }) {
  const awaiting = data.run?.status === "awaiting_approval";
  const p = data.policy;
  const targetW = Math.max(20, width - 40);
  const row = (effect: string, color: string, id: string, action: string, target: string, note?: string | null) => (
    <Text key={id + target}>
      <Text color={color} bold>
        {pad(effect, 9)}
      </Text>
      <Text color="gray">{pad(id, 16)}</Text>
      {pad(action, 14)}
      {trunc(target, targetW)}
      {note ? <Text color="gray">  {note}</Text> : null}
    </Text>
  );
  return (
    <Box flexDirection="column">
      {p?.kill_switch && <Text color="red">Run stopped. Every request is blocked.</Text>}
      {awaiting && (
        <>
          <Text bold>Preview from your decisions</Text>
          {[...(data.plan?.sources ?? [])].map((s) => {
            const d = decisionOf(s, drafts, true);
            return d === "approved"
              ? row("ALLOW", "green", s.source_id, "fetch", s.domain, s.tier === "priority" ? null : "Auto-approved")
              : d === "rejected"
                ? row("DENY", "red", s.source_id, "fetch", s.domain, "Rejected")
                : row("BLOCKED", "yellow", s.source_id, "fetch", s.domain, "Undecided");
          })}
        </>
      )}
      <Box marginTop={awaiting ? 1 : 0}>
        <Text bold>
          In force <Text color="gray">{p ? `version ${p.version}, ${p.rules.length} rules` : "loading"}</Text>
        </Text>
      </Box>
      {p?.rules.map((r) => row(r.effect.toUpperCase(), r.effect === "allow" ? "green" : "red", r.rule_id, r.action, r.action === "model.invoke" ? (r.model_id ?? "any model") : `${r.domain ?? "any domain"}${r.path_prefix ?? ""}`, r.note))}
      {p && p.rules.length === 0 && <Text color="gray">No rules yet.</Text>}
      {row("DENY", "red", "default", "any", "Everything else")}
    </Box>
  );
}
