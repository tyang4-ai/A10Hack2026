"use client";

import { useEffect, useRef, useState } from "react";
import { agentApi, NotSupported, type AgentMessage } from "@/lib/agentApi";
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

// One agent: what it is doing, its traffic, a chat with it, and a stop for just this agent.
export function AgentInspector({ id }: { id: string }) {
  const agent = useRunStore((s) => s.agents[id]);
  const runId = useRunStore((s) => s.runId)!;
  const runStatus = useRunStore((s) => s.run?.status);
  const traffic = useRunStore((s) => s.traffic);
  const log = useRunStore((s) => s.log);
  const live = useRunStore((s) => s.agentMessages[id]);
  const [history, setHistory] = useState<AgentMessage[]>([]);
  const [unsupported, setUnsupported] = useState<string | null>(null);

  // Load the thread once; new messages then arrive as agent.message events.
  useEffect(() => {
    setHistory([]);
    setUnsupported(null);
    agentApi.messages(runId, id).then(
      (r) => setHistory(r.messages),
      (e) => e instanceof NotSupported && setUnsupported(e.message),
    );
  }, [runId, id]);

  if (!agent) return <p className="p-3.5 text-desk-muted italic">{id} (missing)</p>;

  const seen = new Set<string>();
  const thread = [...history, ...(live ?? [])].filter((m) => !seen.has(m.message_id) && seen.add(m.message_id));
  const reqs = traffic.filter((t) => t.agent_id === id);
  const blocked = reqs.filter((t) => t.data.decision !== "allowed").length;
  const notes = log.filter((e) => e.agent_id === id && e.type === "agent.log").slice(-4);
  const runLive = runStatus === "running" || runStatus === "awaiting_approval";

  return (
    <>
      <div className="border-b border-desk-line px-3.5 py-3">
        <div className="flex items-center gap-2">
          <span className="font-mono text-white">{agent.agent_id}</span>
          <span className="text-desk-muted">{agent.role}</span>
          <span className={`ml-auto font-mono text-[11px] font-semibold ${STATUS_CLS[agent.status]}`}>{agent.status.toUpperCase()}</span>
        </div>
        <div className="mt-2 text-[14px] text-white">{agent.task}</div>
        <div className="mt-1 text-desk-muted">
          {agent.model_id ? <span className="font-mono">{agent.model_id}</span> : "Code only, no model"}
          {agent.target_ids.length > 0 && <span>, working on {agent.target_ids.join(", ")}</span>}
        </div>
      </div>

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

      <Chat runId={runId} agentId={id} thread={thread} unsupported={unsupported} onUnsupported={setUnsupported} runStopped={runStatus === "stopped"} />

      {(agent.status === "working" || agent.status === "queued") && runLive && <StopAgent runId={runId} agentId={id} />}
    </>
  );
}

function Chat({
  runId,
  agentId,
  thread,
  unsupported,
  onUnsupported,
  runStopped,
}: {
  runId: string;
  agentId: string;
  thread: AgentMessage[];
  unsupported: string | null;
  onUnsupported: (m: string) => void;
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
      const m = await agentApi.send(runId, agentId, text.trim());
      useRunStore.setState((s) => {
        const list = s.agentMessages[agentId] ?? [];
        return { agentMessages: { ...s.agentMessages, [agentId]: [...list.filter((x) => x.message_id !== m.message_id), m] } };
      });
      setText("");
    } catch (e) {
      if (e instanceof NotSupported) onUnsupported(e.message);
      else setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title={`Talk to ${agentId}`}>
      <div className="grid max-h-72 gap-2 overflow-y-auto" aria-live="polite">
        {thread.length === 0 && <div className="text-desk-muted">Ask this agent about its task, for example why it chose a quote or a number.</div>}
        {thread.map((m) => (
          <div key={m.message_id} className={`max-w-[85%] rounded-[6px] px-2.5 py-1.5 ${m.author === "analyst" ? "ml-auto bg-desk-ink text-desk" : "bg-desk-raised text-desk-ink"}`}>
            <div className="text-[13px] leading-snug whitespace-pre-wrap">{m.text}</div>
            <div className={`mt-0.5 font-mono text-[10px] ${m.author === "analyst" ? "text-muted" : "text-desk-muted"}`}>
              {m.author === "analyst" ? "You" : agentId} · {fmtTime(m.created_at)}
              {m.status === "failed" && <span className="text-desk-bad"> · failed</span>}
            </div>
          </div>
        ))}
        {waiting && <div className="animate-pulse text-desk-muted">{agentId} is answering…</div>}
        <div ref={end} />
      </div>

      {unsupported ? (
        <div role="status" className="mt-2 rounded-[4px] border border-desk-warn/50 px-2.5 py-2 text-desk-warn">
          This backend can't talk to agents yet. The endpoint is proposed in frontend/CONTRACT_REQUESTS.md and needs Gordon to add it to the contract.
        </div>
      ) : (
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
            placeholder={runStopped ? "The run is stopped." : `Message ${agentId}. Enter to send, Shift+Enter for a new line.`}
            className="w-full resize-y rounded-[4px] border border-desk-line bg-desk-raised px-2 py-1.5 text-desk-ink outline-none placeholder:text-desk-muted focus:border-desk-muted"
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
      )}
    </Section>
  );
}

function StopAgent({ runId, agentId }: { runId: string; agentId: string }) {
  const [state, setState] = useState<"idle" | "confirm" | "busy" | "unsupported">("idle");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (state !== "confirm") return;
    const t = setTimeout(() => setState("idle"), 4000);
    return () => clearTimeout(t);
  }, [state]);
  return (
    <Section title="Stop only this agent">
      <p className="mb-2 text-desk-muted">The gateway denies this agent's next request. The rest of the run keeps going.</p>
      {state === "unsupported" ? (
        <div role="status" className="rounded-[4px] border border-desk-warn/50 px-2.5 py-2 text-desk-warn">
          This backend can't stop a single agent yet. It's proposed in frontend/CONTRACT_REQUESTS.md.
        </div>
      ) : (
        <button
          disabled={state === "busy"}
          onClick={async () => {
            if (state === "idle") return setState("confirm");
            setState("busy");
            setErr(null);
            try {
              const a = await agentApi.stop(runId, agentId, "Stopped by the analyst from the inspector");
              useRunStore.setState((s) => ({ agents: { ...s.agents, [a.agent_id]: a } }));
              setState("idle");
            } catch (e) {
              if (e instanceof NotSupported) {
                setState("unsupported");
              } else {
                setErr(e instanceof Error ? e.message : String(e));
                setState("idle");
              }
            }
          }}
          className={`h-7 rounded-[4px] border border-desk-bad px-3 text-[12px] font-semibold transition-colors ${state === "confirm" ? "bg-desk-bad text-desk" : "text-desk-bad hover:bg-desk-bad hover:text-desk"}`}
        >
          {state === "confirm" ? `Confirm stop ${agentId}` : `Stop ${agentId}`}
        </button>
      )}
      {err && (
        <div role="alert" className="mt-1 text-desk-bad">
          {err}
        </div>
      )}
    </Section>
  );
}
