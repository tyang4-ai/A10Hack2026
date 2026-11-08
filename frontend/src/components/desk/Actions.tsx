"use client";

import { useState } from "react";
import { api, type BlockedAction, type Claim, type ClaimAction, type RunEvent } from "@/lib/api";
import { failedChecks, isDropped, isWorking } from "@/lib/ui";

type Opt<A> = { action: A; label: string; primary?: boolean; needsText?: boolean };

// Shared action bar: buttons plus an optional note to the agent.
function Bar<A extends string>({
  options,
  onAct,
  dark,
  initialText,
}: {
  options: Opt<A>[];
  onAct: (a: A, note: string, text?: string) => Promise<unknown>;
  dark?: boolean;
  initialText?: string;
}) {
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(initialText ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = async (o: Opt<A>) => {
    if (o.needsText && !editing) {
      setEditing(true);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await onAct(o.action, note.trim(), o.needsText ? text.trim() : undefined);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const base = dark
    ? "border px-2 py-1 font-mono text-[11px] tracking-[0.06em] transition-colors disabled:opacity-40"
    : "border px-2 py-1 font-mono text-[11px] tracking-[0.06em] transition-colors disabled:opacity-40";
  const tone = (o: Opt<A>) =>
    dark
      ? o.primary
        ? "border-amber bg-amber text-desk hover:bg-[#ffc640]"
        : "border-[#555] text-desk-ink hover:border-desk-ink"
      : o.primary
        ? "border-ink bg-ink text-white hover:bg-[#222]"
        : "border-ink hover:bg-ink hover:text-white";
  return (
    <div className="mt-2 flex flex-col gap-1.5">
      {editing && (
        <textarea
          aria-label="New wording"
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={2}
          className={`w-full resize-y border px-2 py-1 font-serif text-[15px] outline-none ${dark ? "border-[#555] bg-transparent text-white" : "border-ink bg-white"}`}
        />
      )}
      <input
        aria-label="Note to the agent"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Note to the agent (optional)"
        className={`w-full border-b bg-transparent py-1 font-mono text-[11px] outline-none ${dark ? "border-[#555] text-desk-ink placeholder:text-desk-muted" : "border-rule placeholder:text-muted"}`}
      />
      <div className="flex flex-wrap gap-1.5">
        {options.map((o) => (
          <button key={o.action} disabled={busy || (o.needsText && editing && !text.trim())} onClick={() => run(o)} className={`${base} ${tone(o)}`}>
            {o.needsText && editing ? "SAVE EDIT" : o.label}
          </button>
        ))}
        {editing && (
          <button onClick={() => setEditing(false)} className={`${base} ${dark ? "border-[#555] text-desk-muted" : "border-rule text-muted"}`}>
            CANCEL
          </button>
        )}
      </div>
      {err && <div className={`font-mono text-[11px] ${dark ? "text-desk-bad" : "text-bad"}`}>{err}</div>}
    </div>
  );
}

export function blockedOptions(ev: RunEvent): Opt<BlockedAction>[] {
  if (ev.url) return [{ action: "add_source", label: "ADD AS SOURCE", primary: true }, { action: "dismiss", label: "DISMISS" }];
  if (ev.source_id) return [{ action: "allow", label: "ALLOW (PROMOTE)", primary: true }, { action: "dismiss", label: "KEEP BLOCKED" }];
  return [{ action: "dismiss", label: "ACKNOWLEDGE" }];
}

// Resolution text for a blocked event, if a person has settled it.
export function resolutionOf(ev: RunEvent, events: RunEvent[]) {
  return events.find((e) => e.type === "resolution" && e.resolves === ev.seq) ?? null;
}

export function BlockedActions({ runId, event, events, refresh, dark }: { runId: string; event: RunEvent; events: RunEvent[]; refresh: () => Promise<void>; dark?: boolean }) {
  const res = resolutionOf(event, events);
  if (res) {
    return (
      <div className={`mt-1 font-mono text-[11px] ${res.decision === "allow" ? (dark ? "text-desk-good" : "text-good") : dark ? "text-desk-muted" : "text-muted"}`}>
        RESOLVED · {res.actor.id} · {res.summary}
      </div>
    );
  }
  return <Bar dark={dark} options={blockedOptions(event)} onAct={(a, note) => api.resolveBlocked(runId, event.seq, a, note).then(refresh)} />;
}

export function claimOptions(c: Claim): Opt<ClaimAction>[] {
  const s = c.code_check.status;
  if (s === "tier_violation")
    return [
      { action: "send_back", label: "FIND TIER 1 SUPPORT", primary: true },
      { action: "promote_source", label: `PROMOTE ${c.citations[0]?.source_id ?? "SOURCE"}` },
      { action: "drop", label: "DROP" },
    ];
  if (s === "not_found" || s === "value_mismatch" || s === "source_removed")
    return [
      { action: "send_back", label: "SEND BACK TO AGENT", primary: true },
      { action: "drop", label: "DROP" },
    ];
  return [
    { action: "send_back", label: "ASK FOR REWRITE", primary: true },
    ...(c.kind !== "data" ? [{ action: "edit" as const, label: "EDIT", needsText: true }] : []),
    { action: "drop", label: "DROP" },
  ];
}

export function ClaimActions({ runId, claim, refresh, dark }: { runId: string; claim: Claim; refresh: () => Promise<void>; dark?: boolean }) {
  const muted = dark ? "text-desk-muted" : "text-muted";
  if (isWorking(claim)) {
    return <div className={`mt-2 animate-pulse font-mono text-[11px] ${dark ? "text-amber" : ""}`}>AGENT WORKING · {claim.resolution?.action.replace("_", " ")} requested by {claim.resolution?.by}…</div>;
  }
  if (isDropped(claim)) {
    return (
      <div className={`mt-2 font-mono text-[11px] ${muted}`}>
        DROPPED by {claim.resolution?.by}
        {claim.resolution?.note && ` · “${claim.resolution.note}”`}
      </div>
    );
  }
  if (!failedChecks(claim)) {
    return claim.resolution ? (
      <div className={`mt-2 font-mono text-[11px] ${dark ? "text-desk-good" : "text-good"}`}>
        FIXED · {claim.resolution.action.replace("_", " ")} by {claim.resolution.by}
        {claim.resolution.note && ` · “${claim.resolution.note}”`}
      </div>
    ) : null;
  }
  return (
    <Bar
      dark={dark}
      options={claimOptions(claim)}
      initialText={claim.text}
      onAct={(a, note, text) => api.actOnClaim(runId, claim.id, a, { note, text }).then(refresh)}
    />
  );
}
