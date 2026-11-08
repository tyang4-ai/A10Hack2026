"use client";

// What goes inside each kind of window, plus its icon, title and subtitle.
// Every piece subscribes only to the store slice it shows.
import { useEffect, useState } from "react";
import { allSentences, useRunStore, type Selection } from "@/lib/store";
import type { Win, WinKind } from "@/lib/windows";
import { AgentInspector, StopAgentButton } from "./AgentInspector";
import { BriefModule } from "./BriefModule";
import { ChainTree, Inspector, InspectorBody } from "./Inspector";
import { PolicyWindow } from "./PolicyWindow";
import { ReportModule } from "./report";
import { Agents, Log, Traffic, Vault } from "./RunPanels";
import { DocPage, FollowPage, parsePageRef } from "./SourcePage";
import { SourcesModule } from "./SourcesModule";
import { CertaintyPill, Skeleton } from "./ui";

export const KIND_META: Record<WinKind, { icon: string; title: string; paper?: boolean }> = {
  sources: { icon: "1", title: "Sources", paper: true },
  report: { icon: "2", title: "Agent report", paper: true },
  brief: { icon: "3", title: "Brief", paper: true },
  inspector: { icon: "◎", title: "Inspector" },
  traffic: { icon: "⇄", title: "Traffic" },
  agents: { icon: "◍", title: "Agents" },
  vault: { icon: "▤", title: "Vault" },
  log: { icon: "≡", title: "Log" },
  policy: { icon: "◇", title: "Gateway policy" },
  chain: { icon: "⋮", title: "Chain" },
  agent: { icon: "◌", title: "Agent chat" },
  page: { icon: "¶", title: "Source page", paper: true },
  item: { icon: "▣", title: "Item" },
};

// The dock and the window list sort by this order.
export const KIND_ORDER: WinKind[] = ["sources", "report", "brief", "policy", "inspector", "chain", "traffic", "agents", "vault", "log", "agent", "page", "item"];

// "item:D-028" → { type: "item", id: "D-028" }
export function parseItemRef(ref: string | undefined): Selection {
  if (!ref) return null;
  const i = ref.indexOf(":");
  return { type: ref.slice(0, i) as NonNullable<Selection>["type"], id: ref.slice(i + 1) };
}

export function winTitle(w: Pick<Win, "kind" | "ref">): string {
  if (w.kind === "agent" && w.ref) return w.ref;
  if (w.kind === "item") return parseItemRef(w.ref)?.id ?? "Item";
  return KIND_META[w.kind].title;
}

// The unpinned agent chat follows the busiest agent: the newest one that is working.
function useFollowAgent(): string | null {
  return useRunStore((s) => {
    const all = Object.values(s.agents);
    return [...all].reverse().find((a) => a.status === "working")?.agent_id ?? all[all.length - 1]?.agent_id ?? null;
  });
}

export function WindowBody({ kind, wref }: { kind: WinKind; wref?: string }) {
  const ready = useRunStore((s) => !!s.run && !s.loading);
  const runId = useRunStore((s) => s.runId);
  if (!ready || !runId)
    return (
      <div className="p-4">
        <Skeleton rows={5} />
      </div>
    );
  switch (kind) {
    case "sources":
      return (
        <div className="px-6 py-5">
          <SourcesModule />
        </div>
      );
    case "report":
      return (
        <div className="px-6 py-5">
          <ReportModule />
        </div>
      );
    case "brief":
      return (
        <div className="px-6 py-5 print:p-0">
          <BriefModule />
        </div>
      );
    case "inspector":
      return <Inspector />;
    case "traffic":
      return <Traffic />;
    case "agents":
      return <Agents />;
    case "vault":
      return <Vault />;
    case "log":
      return <Log />;
    case "policy":
      return <PolicyWindow />;
    case "chain":
      return <ChainWindow />;
    case "agent":
      return wref ? <AgentInspector id={wref} hideStop /> : <FollowAgent />;
    case "page": {
      if (!wref) return <FollowPage />;
      const p = parsePageRef(wref);
      return <DocPage key={wref} runId={runId} docId={p.doc} startPage={p.page} itemId={p.item} />;
    }
    case "item":
      return <InspectorBody sel={parseItemRef(wref)} />;
  }
}

function FollowAgent() {
  const id = useFollowAgent();
  if (!id) return <p className="p-3.5 text-[13px] text-desk-muted">No agents yet. This window follows the busiest agent once the run starts.</p>;
  return <AgentInspector id={id} hideStop />;
}

// The citation chain for the selected brief sentence. It remembers the last
// sentence, so following the chain down to an item keeps the chain on screen.
function ChainWindow() {
  const selected = useRunStore((s) => s.selected);
  const items = useRunStore((s) => s.items);
  const summary = useRunStore((s) => s.summary);
  const runId = useRunStore((s) => s.runId);
  const [sid, setSid] = useState<string | null>(null);
  useEffect(() => {
    if (selected?.type === "sentence") setSid(selected.id);
  }, [selected]);
  useEffect(() => setSid(null), [runId]);
  const s = sid ? allSentences(summary).find((x) => x.id === sid) : null;
  if (!s) return <p className="p-3.5 text-[13px] text-desk-muted">Click a sentence in the brief to follow it down to the source quotes.</p>;
  return (
    <div className="text-[12px] text-desk-ink">
      <div className="border-b border-desk-line px-3.5 py-2.5">
        <div className="flex items-center gap-2">
          <span className="font-mono text-white">{s.id}</span>
          <CertaintyPill c={s.certainty} />
          <span className="text-desk-muted">Click a step to open its source page.</span>
        </div>
        <div className="mt-1.5 font-serif text-[15px] text-white">{s.text}</div>
      </div>
      <div className="px-3.5 py-2.5">
        <ChainTree refs={s.derived_from} items={items} depth={0} />
      </div>
    </div>
  );
}

// ---- subtitles: short and muted, each with its own narrow subscription ----

export function WinSubtitle({ kind, wref }: { kind: WinKind; wref?: string }) {
  switch (kind) {
    case "sources":
      return <SourcesSub />;
    case "report":
      return <ReportSub />;
    case "brief":
      return <BriefSub />;
    case "inspector":
      return <InspectorSub />;
    case "traffic":
      return <TrafficSub />;
    case "agents":
      return <AgentsSub />;
    case "agent":
      return wref ? <AgentSub id={wref} /> : <FollowAgentSub />;
    case "page":
      return <>{wref ? `${parsePageRef(wref).doc}, page ${parsePageRef(wref).page}` : "follows the selection"}</>;
    case "chain":
      return <>follows the selected sentence</>;
    case "policy":
      return <PolicySub />;
    case "item":
      return <>{parseItemRef(wref)?.type ?? ""}</>;
    case "vault":
      return <>raw files and transcripts</>;
    case "log":
      return <LogSub />;
  }
}

function SourcesSub() {
  const n = useRunStore((s) => Object.keys(s.sources).length);
  return <>{n} sources</>;
}
function ReportSub() {
  const status = useRunStore((s) => s.report?.status);
  const n = useRunStore((s) => Object.keys(s.items).length);
  return <>{status ? `${status.replace("_", " ")}, ${n} items` : ""}</>;
}
function BriefSub() {
  const status = useRunStore((s) => s.summary?.status);
  return <>{status ? status.replace("_", " ") : ""}</>;
}
function InspectorSub() {
  const id = useRunStore((s) => s.selected?.id);
  return <>{id ?? "follows the selection"}</>;
}
function TrafficSub() {
  const n = useRunStore((s) => s.traffic.length);
  return <>{n} requests</>;
}
function AgentsSub() {
  const n = useRunStore((s) => Object.keys(s.agents).length);
  return <>{n} agents</>;
}
function AgentSub({ id }: { id: string }) {
  const a = useRunStore((s) => s.agents[id]);
  return <>{a ? `${a.role}, ${a.status}` : ""}</>;
}
function FollowAgentSub() {
  const id = useFollowAgent();
  return <>{id ? `following ${id}` : "follows the busiest agent"}</>;
}
function PolicySub() {
  const p = useRunStore((s) => s.policy);
  const awaiting = useRunStore((s) => s.run?.status === "awaiting_approval");
  return <>{awaiting ? "preview while you decide" : p ? `version ${p.version}` : ""}</>;
}
function LogSub() {
  const n = useRunStore((s) => s.log.length);
  return <>{n} events</>;
}

// Extra title bar controls. The agent chat gets "Stop agent" while the agent can still be stopped.
export function TitleExtras({ kind, wref }: { kind: WinKind; wref?: string }) {
  if (kind !== "agent") return null;
  return wref ? <AgentStop id={wref} /> : <FollowAgentStop />;
}
function FollowAgentStop() {
  const id = useFollowAgent();
  return id ? <AgentStop id={id} /> : null;
}
function AgentStop({ id }: { id: string }) {
  const runId = useRunStore((s) => s.runId);
  const can = useRunStore((s) => {
    const a = s.agents[id];
    const live = s.run?.status === "running" || s.run?.status === "awaiting_approval";
    return !!a && live && (a.status === "working" || a.status === "queued");
  });
  if (!can || !runId) return null;
  return <StopAgentButton key={id} runId={runId} agentId={id} compact />;
}
