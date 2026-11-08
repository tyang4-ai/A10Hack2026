"use client";

import { useEffect, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { isEvent, type AgentMessage } from "@contract/types";
import { useWindows } from "@/lib/windows";
import { api } from "@/lib/api";
import { count, fmtTime, usd } from "@/lib/format";
import { useRunStore } from "@/lib/store";

const STATUS_CLS = { queued: "text-desk-muted", working: "text-desk-link", done: "text-desk-good", failed: "text-desk-bad", stopped: "text-desk-warn" } as const;

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-desk-line px-3.5 py-3">
      <div className="mb-1.5 text-[11px] font-semibold text-desk-muted">{title}</div>
      {children}
    </div>
  );
}

const CALL_CLS = { proceed: "text-desk-good", rework: "text-desk-warn", escalate: "text-desk-bad" } as const;

// Bobby's calls at each step: proceed, rework (with the goons he sent back) or escalate.
function Decisions() {
  const calls = useRunStore(useShallow((s) => s.log.filter((e) => isEvent(e, "agent.decision"))));
  const open = useWindows((s) => s.open);
  return (
    <Section title={`Bobby's calls (${calls.length})`}>
      {calls.length === 0 && <div className="text-desk-muted">No calls yet.</div>}
      <ol className="grid gap-2">
        {calls.map((e) =>
          isEvent(e, "agent.decision") ? (
            <li key={e.seq}>
              <div>
                <span className="font-mono text-desk-muted">{fmtTime(e.ts)}</span>{" "}
                <span className={`font-mono text-[11px] font-semibold ${CALL_CLS[e.data.decision]}`}>{e.data.decision.toUpperCase()}</span>{" "}
                <span className="text-desk-muted">after {e.data.stage.replace("_", " ")}</span>
              </div>
              <div className="text-white">{e.data.summary}</div>
              {e.data.reworks.map((w) => (
                <div key={w.agent_id} className="text-desk-muted">
                  Sent back{" "}
                  <button onClick={() => open("agent", w.agent_id)} className="font-mono text-desk-link hover:underline">
                    {w.agent_id}
                  </button>
                  : {w.instructions}
                </div>
              ))}
            </li>
          ) : null,
        )}
      </ol>
    </Section>
  );
}

// One agent: what it is doing, its traffic, a chat with it, and a stop for just this agent.
export function AgentInspector({ id, hideStop }: { id: string; hideStop?: boolean }) {
  const agent = useRunStore((s) => s.agents[id]);
  const runId = useRunStore((s) => s.runId)!;
  const runStatus = useRunStore((s) => s.run?.status);
  const traffic = useRunStore((s) => s.traffic);
  const log = useRunStore((s) => s.log);
  const live = useRunStore((s) => s.agentMessages[id]);
  const [history, setHistory] = useState<AgentMessage[]>([]);

  // Load the thread once; new messages then arrive as agent.message events.
  useEffect(() => {
    setHistory([]);
    api.agentMessages(runId, id).then(
      (r) => setHistory(r.messages),
      () => setHistory([]),
    );
  }, [runId, id]);

  if (!agent) return <p className="p-3.5 text-desk-muted italic">{id} (missing)</p>;

  const seen = new Set<string>();
  // Newest copy of each message wins (a question comes back as "answered"), then oldest first,
  // with the analyst's question before the reply when they share a timestamp.
  const thread = [...(live ?? []), ...history]
    .filter((m) => !seen.has(m.message_id) && seen.add(m.message_id))
    .sort((a, b) => a.created_at.localeCompare(b.created_at) || (a.author === "analyst" ? -1 : 1));
  const reqs = traffic.filter((t) => t.agent_id === id);
  const blocked = reqs.filter((t) => t.data.decision !== "allowed").length;
  const notes = log.filter((e) => e.agent_id === id && e.type === "agent.log").slice(-4);
  const runLive = runStatus === "running" || runStatus === "awaiting_approval";

  return (
    <>
      <div className="border-b border-desk-line px-3.5 py-3">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-white">{agent.name ?? agent.agent_id}</span>
          {agent.name && <span className="font-mono text-desk-muted">{agent.agent_id}</span>}
          <span className="text-desk-muted">{agent.role}</span>
          <span className={`ml-auto font-mono text-[11px] font-semibold ${STATUS_CLS[agent.status]}`}>{agent.status.toUpperCase()}</span>
        </div>
        <div className="mt-2 text-[14px] text-white">{agent.task}</div>
        <div className="mt-1 text-desk-muted">
          {agent.model_id ? <span className="font-mono">{agent.model_id}</span> : "Code only, no model"}
          {agent.target_ids.length > 0 && <span>, working on {agent.target_ids.join(", ")}</span>}
        </div>
        {agent.parent_agent_id && (
          <div className="mt-2 rounded-[4px] border border-desk-line-strong px-2.5 py-1.5">
            Rerun of <span className="font-mono">{agent.parent_agent_id}</span>
            {agent.rerun_by === "orchestrator" ? " ordered by Bobby" : agent.rerun_by === "analyst" ? " by you" : ""}
            {agent.instructions && <div className="text-desk-muted">“{agent.instructions}”</div>}
          </div>
        )}
      </div>

      {agent.role === "orchestrator" && <Decisions />}

      <Section title="Usage">
        <div className="font-mono tabular-nums">
          {agent.usage.model_calls} calls, {count(agent.usage.tokens_in)} in / {count(agent.usage.tokens_out)} out, {usd(agent.usage.cost_usd)}
        </div>
        <div className="mt-1 text-desk-muted">
          {reqs.length} gateway requests
          {blocked > 0 && <span className="text-desk-bad">, {blocked} blocked or flagged</span>}
        </div>
        {agent.transcript_path && <div className="mt-1 break-all font-mono text-[11px] text-desk-muted">{agent.transcript_path}</div>}
      </Section>

      {notes.length > 0 && (
        <Section title="What it noted">
          {notes.map((e) => (
            <div key={e.seq} className="mb-1">
              <span className="font-mono text-desk-muted">{fmtTime(e.ts)}</span> {e.message}
            </div>
          ))}
        </Section>
      )}

      <Chat runId={runId} agentId={id} thread={thread} runStopped={runStatus === "stopped"} />

      {!hideStop && (agent.status === "working" || agent.status === "queued") && runLive && (
        <Section title="Stop only this agent">
          <p className="mb-2 text-desk-muted">The rest of the run keeps going.</p>
          <StopAgentButton runId={runId} agentId={id} />
        </Section>
      )}
    </>
  );
}

function Chat({
  runId,
  agentId,
  thread,
  runStopped,
}: {
  runId: string;
  agentId: string;
  thread: AgentMessage[];
  runStopped: boolean;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const end = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    // Block body on purpose: newer browsers make scrollIntoView return a Promise,
    // and an effect must not return anything but a cleanup function.
    void end.current?.scrollIntoView({ block: "nearest" });
  }, [thread.length]);
  const waiting = thread.length > 0 && thread[thread.length - 1].author === "analyst" && thread[thread.length - 1].status === "sent";

  const send = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const m = await api.sendAgentMessage(runId, agentId, text.trim());
      useRunStore.setState((s) => {
        const list = s.agentMessages[agentId] ?? [];
        return { agentMessages: { ...s.agentMessages, [agentId]: [...list.filter((x) => x.message_id !== m.message_id), m] } };
      });
      setText("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title={`Talk to ${agentId}`}>
      <div className="grid max-h-72 gap-2 overflow-y-auto" aria-live="polite">
        {thread.length === 0 && <div className="text-desk-muted">Ask {agentId} about its work.</div>}
        {thread.map((m) => (
          <div key={m.message_id} className={`max-w-[85%] rounded-[6px] px-2.5 py-1.5 ${m.author === "analyst" ? "ml-auto bg-desk-ink text-desk" : "bg-desk-raised text-desk-ink"}`}>
            <div className="text-[13px] leading-snug whitespace-pre-wrap">{m.text}</div>
            <div className={`mt-0.5 font-mono text-[10px] ${m.author === "analyst" ? "text-muted" : "text-desk-muted"}`}>
              {m.author === "analyst" ? "You" : agentId} · {fmtTime(m.created_at)}
              {m.author === "agent" && <span title="Chat replies are not verified and never change the report"> · not checked</span>}
              {m.status === "failed" && <span className="text-desk-bad"> · failed</span>}
            </div>
          </div>
        ))}
        {waiting && <div className="animate-pulse text-desk-muted">{agentId} is answering…</div>}
        <div ref={end} />
      </div>

        <form
          className="mt-2"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <label className="sr-only" htmlFor={`chat-${agentId}`}>
            Message to {agentId}
          </label>
          <textarea
            id={`chat-${agentId}`}
            rows={2}
            value={text}
            disabled={runStopped}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            placeholder={runStopped ? "The run is stopped." : `Message ${agentId}`}
            className="w-full resize-y rounded-[4px] border border-desk-line bg-desk-raised px-2 py-1.5 text-desk-ink placeholder:text-desk-muted focus:border-desk-muted"
          />
          <button disabled={busy || !text.trim() || runStopped} className="mt-1.5 h-7 rounded-[4px] bg-desk-ink px-3 text-[12px] font-medium text-desk disabled:opacity-40">
            Send
          </button>
          {err && (
            <div role="alert" className="mt-1 text-desk-bad">
              {err}
            </div>
          )}
        </form>
    </Section>
  );
}

// Two clicks: the first asks to confirm, the second stops. Also used in the agent chat window's title bar.
export function StopAgentButton({ runId, agentId, compact }: { runId: string; agentId: string; compact?: boolean }) {
  const [state, setState] = useState<"idle" | "confirm" | "busy">("idle");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (state !== "confirm") return;
    const t = setTimeout(() => setState("idle"), 4000);
    return () => clearTimeout(t);
  }, [state]);
  return (
    <span className="inline-flex items-center gap-2">
      <button
        disabled={state === "busy"}
        onClick={async () => {
          if (state === "idle") return setState("confirm");
          setState("busy");
          setErr(null);
          try {
            const a = await api.stopAgent(runId, agentId, "Stopped by the analyst from the dashboard");
            useRunStore.setState((s) => ({ agents: { ...s.agents, [a.agent_id]: a } }));
            setState("idle");
          } catch (e) {
            setErr(e instanceof Error ? e.message : String(e));
            setState("idle");
          }
        }}
        className={`h-6 rounded-[4px] border border-desk-bad px-2.5 text-[12px] font-semibold transition-colors ${state === "confirm" ? "bg-desk-bad text-desk" : "text-desk-bad hover:bg-desk-bad hover:text-desk"}`}
      >
        {state === "confirm" ? (compact ? "Confirm stop" : `Confirm stop ${agentId}`) : compact ? "Stop agent" : `Stop ${agentId}`}
      </button>
      {err && (
        <span role="alert" className="text-desk-bad">
          {err}
        </span>
      )}
    </span>
  );
}
