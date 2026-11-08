import { useState } from "react";
import type { RunEvent } from "../api";
import { fmtTime } from "../ui";

const KIND_LABEL = { human: "H", agent: "A", code: "C", gateway: "GW" } as const;

export function Traffic({ events }: { events: RunEvent[] }) {
  const [filter, setFilter] = useState<"all" | "blocked" | "calls">("all");
  const shown = events
    .filter((e) =>
      filter === "all" ? e.type !== "step" :
      filter === "blocked" ? e.decision === "deny" :
      e.type === "model_call" || e.type === "fetch",
    )
    .slice()
    .reverse();

  return (
    <aside className="traffic" aria-label="Gateway traffic">
      <header>
        <h2>Gateway traffic</h2>
        <div className="seg" role="tablist">
          {(["all", "calls", "blocked"] as const).map((f) => (
            <button key={f} role="tab" aria-selected={filter === f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>
              {f === "all" ? "All" : f === "calls" ? "Calls" : `Blocked (${events.filter((e) => e.decision === "deny").length})`}
            </button>
          ))}
        </div>
      </header>
      <ol className="feed">
        {shown.length === 0 && <li className="empty">No events yet.</li>}
        {shown.map((e) => (
          <li key={e.seq} className={`ev ev-${e.type} ${e.decision === "deny" ? "ev-deny" : ""}`}>
            <div className="ev-meta">
              <span className={`who who-${e.actor.kind}`} title={e.actor.kind}>{KIND_LABEL[e.actor.kind]}</span>
              <span className="ev-actor">{e.actor.id}</span>
              <span className="ev-type">{e.type.replace("_", " ")}</span>
              <time>{fmtTime(e.ts)}</time>
            </div>
            <div className="ev-sum">
              {e.decision === "deny" && <strong className="deny">BLOCKED </strong>}
              {e.summary}
            </div>
            {(e.latency_ms || e.tokens) && (
              <div className="ev-num">
                {e.latency_ms ? `${e.latency_ms} ms` : ""}
                {e.latency_ms && e.tokens ? " · " : ""}
                {e.tokens ? `${e.tokens.toLocaleString()} tok` : ""}
              </div>
            )}
          </li>
        ))}
      </ol>
    </aside>
  );
}
