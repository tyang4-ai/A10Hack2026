"use client";

// One normalized store for a run. Snapshots load first; events then update it.
// Each snapshot carries as_of_seq, and an event only touches a slice if it is
// newer than that slice's snapshot, so replaying from seq 0 never regresses state.
import { create } from "zustand";
import {
  isEvent,
  type Agent,
  type AgentReport,
  type Block,
  type Comment,
  type DataNeed,
  type Event,
  type GatewayRequestEvent,
  type HumanReport,
  type Item,
  type Policy,
  type Run,
  type Source,
  type SummarySentence,
} from "@contract/types";
import { agentMessageFromEvent, type AgentMessage } from "./agentApi";
import { api } from "./api";
import { moduleFor, type Module } from "./format";

export type Selection = { type: "source" | "item" | "sentence" | "hole" | "agent"; id: string } | null;

type Seqs = { run: number; plan: number; report: number; summary: number; agents: number; policy: number };

interface State {
  runs: Run[];
  runId: string | null;
  run: Run | null;
  needs: DataNeed[];
  sources: Record<string, Source>;
  planReady: boolean;
  items: Record<string, Item>;
  report: Omit<AgentReport, "items"> | null;
  summary: HumanReport | null;
  agents: Record<string, Agent>;
  policy: Policy | null;
  comments: Record<string, Comment>;
  agentMessages: Record<string, AgentMessage[]>;
  traffic: GatewayRequestEvent[];
  log: Event[];
  lastSeq: number;
  seqs: Seqs;
  loading: boolean;
  error: string | null;
  selected: Selection;
  module: Module;

  loadRuns: () => Promise<void>;
  openRun: (id: string | null) => Promise<void>;
  select: (s: Selection) => void;
  setModule: (m: Module) => void;
  apply: (events: Event[]) => void;
  refreshReport: () => Promise<void>;
  refreshSummary: () => Promise<void>;
  refreshPlan: () => Promise<void>;
  refreshPolicy: () => Promise<void>;
  refreshRun: () => Promise<void>;
}

const LOG_CAP = 4000;

const empty = {
  run: null,
  needs: [],
  sources: {},
  planReady: false,
  items: {},
  report: null,
  summary: null,
  agents: {},
  policy: null,
  comments: {},
  agentMessages: {},
  traffic: [],
  log: [],
  lastSeq: 0,
  seqs: { run: 0, plan: 0, report: 0, summary: 0, agents: 0, policy: 0 },
  error: null,
  selected: null,
} satisfies Partial<State>;

// Insert or replace a sentence in the brief, creating its section if needed.
function upsertSentence(summary: HumanReport, heading: string, s: SummarySentence): HumanReport {
  const sections = summary.sections.map((sec) => ({ ...sec, blocks: sec.blocks.map((b) => replaceIn(b, s)) }));
  const found = sections.some((sec) => sec.blocks.some((b) => blockHas(b, s.id)));
  if (!found) {
    let sec = sections.find((x) => x.heading === heading);
    if (!sec) {
      sec = { heading, blocks: [] };
      sections.push(sec);
    }
    const last = sec.blocks[sec.blocks.length - 1];
    if (last && last.type === "paragraph") last.sentences = [...last.sentences, s];
    else sec.blocks.push({ type: "paragraph", sentences: [s] });
  }
  return { ...summary, sections };
}

function blockHas(b: Block, id: string) {
  if (b.type === "paragraph" || b.type === "bullets") return b.sentences.some((s) => s.id === id);
  if (b.type === "chart") return b.caption.id === id;
  return false;
}

function replaceIn(b: Block, s: SummarySentence): Block {
  if (b.type === "paragraph" || b.type === "bullets") return { ...b, sentences: b.sentences.map((x) => (x.id === s.id ? s : x)) };
  if (b.type === "chart" && b.caption.id === s.id) return { ...b, caption: s };
  return b;
}

export function allSentences(summary: HumanReport | null): SummarySentence[] {
  if (!summary) return [];
  return summary.sections.flatMap((sec) =>
    sec.blocks.flatMap((b) => (b.type === "paragraph" || b.type === "bullets" ? b.sentences : b.type === "chart" ? [b.caption] : [])),
  );
}

export const useRunStore = create<State>((set, get) => ({
  runs: [],
  runId: null,
  loading: false,
  module: "sources",
  ...empty,

  loadRuns: async () => {
    const { runs } = await api.listRuns();
    set({ runs });
  },

  openRun: async (id) => {
    set({ ...empty, runId: id, loading: !!id });
    if (!id) return;
    try {
      const [run, plan, report, summary, agents, policy, comments] = await Promise.all([
        api.getRun(id),
        api.plan(id),
        api.report(id),
        api.summary(id),
        api.agents(id).catch(() => null),
        api.policy(id).catch(() => null),
        api.comments(id).catch(() => null),
      ]);
      if (get().runId !== id) return;
      const { items, ...rest } = report;
      set({
        run,
        needs: plan.needs,
        sources: Object.fromEntries(plan.sources.map((s) => [s.source_id, s])),
        planReady: plan.ready,
        items: Object.fromEntries(items.map((i) => [i.id, i])),
        report: rest,
        summary,
        agents: agents ? Object.fromEntries(agents.agents.map((a) => [a.agent_id, a])) : {},
        policy,
        comments: comments ? Object.fromEntries(comments.comments.map((c) => [c.comment_id, c])) : {},
        seqs: {
          run: run.last_seq,
          plan: plan.as_of_seq,
          report: report.as_of_seq,
          summary: summary.as_of_seq,
          agents: agents?.as_of_seq ?? 0,
          policy: policy?.as_of_seq ?? 0,
        },
        module: moduleFor(run.stage),
        loading: false,
      });
    } catch (e) {
      set({ loading: false, error: e instanceof Error ? e.message : String(e) });
    }
  },

  select: (selected) => set({ selected }),
  setModule: (module) => set({ module }),

  refreshRun: async () => {
    const id = get().runId;
    if (!id) return;
    const run = await api.getRun(id);
    if (get().runId === id) set((s) => ({ run, seqs: { ...s.seqs, run: Math.max(s.seqs.run, run.last_seq) } }));
  },
  refreshReport: async () => {
    const id = get().runId;
    if (!id) return;
    const { items, ...rest } = await api.report(id);
    if (get().runId !== id) return;
    set((s) => ({ items: Object.fromEntries(items.map((i) => [i.id, i])), report: rest, seqs: { ...s.seqs, report: rest.as_of_seq } }));
  },
  refreshSummary: async () => {
    const id = get().runId;
    if (!id) return;
    const summary = await api.summary(id);
    if (get().runId === id) set((s) => ({ summary, seqs: { ...s.seqs, summary: summary.as_of_seq } }));
  },
  refreshPlan: async () => {
    const id = get().runId;
    if (!id) return;
    const plan = await api.plan(id);
    if (get().runId !== id) return;
    set((s) => ({
      needs: plan.needs,
      sources: Object.fromEntries(plan.sources.map((x) => [x.source_id, x])),
      planReady: plan.ready,
      seqs: { ...s.seqs, plan: plan.as_of_seq },
    }));
  },
  refreshPolicy: async () => {
    const id = get().runId;
    if (!id) return;
    const policy = await api.policy(id);
    if (get().runId === id) set((s) => ({ policy, seqs: { ...s.seqs, policy: policy.as_of_seq } }));
  },

  // Apply a batch of events in one state update.
  apply: (events) => {
    if (!events.length) return;
    const st = get();
    const seqs = st.seqs;
    let run = st.run;
    let needs = st.needs;
    let planReady = st.planReady;
    const sources = { ...st.sources };
    const items = { ...st.items };
    const agents = { ...st.agents };
    const comments = { ...st.comments };
    const agentMessages = { ...st.agentMessages };
    let summary = st.summary;
    let report = st.report;
    const traffic = [...st.traffic];
    const refetch = new Set<"report" | "summary" | "plan" | "policy" | "run">();
    let lastSeq = st.lastSeq;

    for (const e of events) {
      if (e.seq <= lastSeq) continue;
      lastSeq = e.seq;
      const newer = (k: keyof Seqs) => e.seq > seqs[k];

      if (isEvent(e, "gateway.request")) traffic.push(e);

      if (run && newer("run")) {
        if (isEvent(e, "run.status")) run = { ...run, status: e.data.status, stage: e.data.stage };
        else if (isEvent(e, "run.stopped")) run = { ...run, status: "stopped", stop_reason: e.data.reason };
        else if (isEvent(e, "run.failed")) run = { ...run, status: "failed", error: e.data.error };
        else if (isEvent(e, "run.completed")) run = { ...run, status: "done", stage: "complete" };
        else if (isEvent(e, "usage.updated")) run = { ...run, usage: e.data.usage };
      }

      if (newer("plan")) {
        if (isEvent(e, "plan.need")) needs = needs.some((n) => n.need_id === e.data.need.need_id) ? needs : [...needs, e.data.need];
        else if (isEvent(e, "plan.source")) sources[e.data.source.source_id] = e.data.source;
        else if (isEvent(e, "plan.ready")) planReady = true;
        else if (isEvent(e, "source.status")) {
          const s = sources[e.data.source_id];
          if (s) sources[s.source_id] = { ...s, status: e.data.status, stats: e.data.stats, error: e.data.error };
        } else if (isEvent(e, "approval.committed")) refetch.add("plan");
        else if (isEvent(e, "doc.stored")) {
          const s = sources[e.data.source_id];
          if (s && !s.doc_ids.includes(e.data.doc_id)) sources[s.source_id] = { ...s, doc_ids: [...s.doc_ids, e.data.doc_id] };
        }
      }

      if (newer("policy") && isEvent(e, "policy.updated")) refetch.add("policy");

      if (newer("agents")) {
        if (isEvent(e, "agent.spawned")) agents[e.data.agent.agent_id] = e.data.agent;
        else if (isEvent(e, "agent.status")) {
          const a = agents[e.data.agent_id];
          if (a) agents[a.agent_id] = { ...a, status: e.data.status, task: e.data.task, usage: e.data.usage };
        }
      }

      if (newer("report")) {
        if (isEvent(e, "item.created") || isEvent(e, "item.updated")) items[e.data.item.id] = e.data.item;
        else if (isEvent(e, "item.verified")) {
          const it = items[e.data.item_id];
          if (it) items[it.id] = { ...it, verification: e.data.verification, certainty: e.data.certainty } as Item;
        } else if (isEvent(e, "report.ready")) refetch.add("report");
        if (report && report.status === "not_started" && isEvent(e, "item.created")) report = { ...report, status: "drafting" };
      }

      if (summary && newer("summary")) {
        if (isEvent(e, "summary.sentence")) summary = upsertSentence(summary, e.data.section_heading, e.data.sentence);
        else if (isEvent(e, "summary.verified")) {
          const s = allSentences(summary).find((x) => x.id === e.data.sentence_id);
          if (s) summary = upsertSentence(summary, "", { ...s, verification: e.data.verification, certainty: e.data.certainty });
        } else if (isEvent(e, "summary.ready")) refetch.add("summary");
        if (summary.status === "not_started" && isEvent(e, "summary.sentence")) summary = { ...summary, status: "drafting" };
      }

      if (isEvent(e, "comment.created") || isEvent(e, "comment.resolved")) comments[e.data.comment.comment_id] = e.data.comment;

      // Provisional agent.message event (not in the contract yet, see agentApi.ts).
      const am = agentMessageFromEvent(e as { type: string; data?: unknown });
      if (am) {
        const list = agentMessages[am.agent_id] ?? [];
        agentMessages[am.agent_id] = [...list.filter((m) => m.message_id !== am.message_id), am];
      }
    }

    const log = [...st.log, ...events.filter((e) => e.seq > st.lastSeq)];
    set({
      run,
      needs,
      planReady,
      sources,
      items,
      agents,
      comments,
      agentMessages,
      summary,
      report,
      traffic,
      log: log.length > LOG_CAP ? log.slice(-LOG_CAP) : log,
      lastSeq,
    });
    if (refetch.has("report")) void get().refreshReport();
    if (refetch.has("summary")) void get().refreshSummary();
    if (refetch.has("plan")) void get().refreshPlan();
    if (refetch.has("policy")) void get().refreshPolicy();
  },
}));
