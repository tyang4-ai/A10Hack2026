"use client";

import { useMemo, useState } from "react";
import type { Certainty, CheckResult, DiffSegment, Item } from "@contract/types";
import { api } from "@/lib/api";
import { CERTAINTY_HELP, formatValue, human } from "@/lib/format";
import { allSentences, useRunStore, type Selection } from "@/lib/store";
import { useWindows } from "@/lib/windows";
import { AgentInspector } from "./AgentInspector";
import { itemText } from "./report";
import { CertaintyPill, KindBadge, Label, SourceChip, StatusText } from "./ui";

// The inspector follows the store's selection. Item windows pin it to one thing.
export function Inspector() {
  const selected = useRunStore((s) => s.selected);
  return <InspectorBody sel={selected} />;
}

export function InspectorBody({ sel }: { sel: Selection }) {
  return (
    <div className="text-[12px] text-desk-ink">
      {!sel && <p className="p-3.5 text-[13px] text-desk-muted">Select anything to inspect it.</p>}
      {sel?.type === "item" && <ItemInspector id={sel.id} />}
      {sel?.type === "source" && <SourceInspector id={sel.id} />}
      {sel?.type === "sentence" && <SentenceInspector id={sel.id} />}
      {sel?.type === "agent" && <AgentInspector id={sel.id} />}
      {sel?.type === "hole" && <HoleInspector id={sel.id} />}
    </div>
  );
}

function HoleInspector({ id }: { id: string }) {
  const h = useRunStore((s) => s.report?.holes.find((x) => x.hole_id === id));
  if (!h) return <p className="p-3.5 text-desk-muted italic">{id} (missing)</p>;
  return (
    <>
      <div className="border-b border-desk-line px-3.5 py-3">
        <div className="flex items-center gap-2">
          <span className="font-mono text-white">{h.hole_id}</span>
          <span className="text-desk-muted">declared gap{h.criterion ? `, ${human(h.criterion)}` : ""}</span>
        </div>
        <div className="mt-2 font-serif text-[16px] text-white">{h.reason}</div>
      </div>
      {h.item_id && (
        <Block title="Item">
          <Ref id={h.item_id} />
        </Block>
      )}
    </>
  );
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-desk-line px-3.5 py-3">
      <div className="mb-1.5 text-[11px] font-semibold text-desk-muted">{title}</div>
      {children}
    </div>
  );
}

function Ref({ id }: { id: string }) {
  const items = useRunStore((s) => s.items);
  const select = useRunStore((s) => s.select);
  if (id === "rollup") return <span className="text-desk-muted">rollup (scorecard)</span>;
  if (id.startsWith("HOLE-")) return <span className="text-accent">{id} (declared gap)</span>;
  if (id.startsWith("S-")) return <button onClick={() => select({ type: "source", id })} className="text-desk-link hover:underline">{id}</button>;
  const base = id.split(":")[0];
  const it = items[base];
  if (!it) return <span className="text-desk-muted italic">{id} (missing)</span>;
  return (
    <button onClick={() => select({ type: "item", id: base })} className="text-left text-desk-link hover:underline">
      {id}
    </button>
  );
}

export function CertaintyBlock({ c }: { c: Certainty | null }) {
  if (!c) return null;
  return (
    <Block title="Certainty">
      <div className="flex items-baseline gap-3" title={CERTAINTY_HELP}>
        <span className="text-[26px] text-white">{c.score ?? "—"}</span>
        <span className="capitalize">{c.band}</span>
      </div>
      <div className="mt-1 font-mono tabular-nums">
        support {c.support.toFixed(2)} × inherited {c.inherited.toFixed(2)} × completeness {c.completeness.toFixed(2)}
      </div>
      <div className="mt-1 text-desk-muted">{c.basis}</div>
      {c.weakest_ref && (
        <div className="mt-1">
          Weakest input: <Ref id={c.weakest_ref} />
        </div>
      )}
      <div className="mt-1 text-[10px] text-desk-muted">{CERTAINTY_HELP}</div>
    </Block>
  );
}

export function Diff({ diff }: { diff: DiffSegment[] }) {
  return (
    <div className="mt-1 border border-desk-line bg-black/30 px-2 py-1.5 text-[13px]" aria-label="Difference between the agent's quote and the source">
      {diff.map((d, i) =>
        d.op === "equal" ? (
          <span key={i}>{d.agent}</span>
        ) : (
          <span key={i}>
            {d.agent && <del className="bg-desk-bad/25 text-desk-bad">{d.agent}</del>}
            {d.source && <ins className="bg-desk-good/20 text-desk-good no-underline">{d.source}</ins>}
          </span>
        ),
      )}
      <div className="mt-1 text-[10px] text-desk-muted">
        <del className="text-desk-bad">agent</del> vs <ins className="text-desk-good no-underline">source</ins>
      </div>
    </div>
  );
}

function Checks({ checks }: { checks: CheckResult[] }) {
  return (
    <ul className="grid gap-1.5">
      {checks.map((c, i) => (
        <li key={i}>
          <span className={`font-bold ${c.result === "pass" ? "text-desk-good" : c.result === "fail" ? "text-desk-bad" : "text-desk-warn"}`}>
            {c.result === "pass" ? "✓" : c.result === "fail" ? "✕" : "!"}
          </span>{" "}
          <span className="text-white">{human(c.check)}</span> <span className="text-desk-muted">· {c.method === "llm_judge" ? `judges ${c.votes_for}/${c.votes_total}` : c.method}</span>
          <div className="text-desk-muted">{c.detail}</div>
          {c.diff && <Diff diff={c.diff} />}
          {c.unsupported_spans.length > 0 && <div className="text-desk-bad">Unsupported: {c.unsupported_spans.map((s) => `“${s}”`).join(", ")}</div>}
        </li>
      ))}
    </ul>
  );
}

export function citesOf(it: Item): string[] {
  if (it.kind === "data") return [it.source_id];
  if (it.kind === "conclusion") return it.data_ids;
  if (it.kind === "extrapolation") return it.conclusion_ids;
  return it.refs;
}

function ItemInspector({ id }: { id: string }) {
  const items = useRunStore((s) => s.items);
  const summary = useRunStore((s) => s.summary);
  const runId = useRunStore((s) => s.runId)!;
  const it = items[id];
  const openWin = useWindows((s) => s.open);
  const citedBy = useMemo(() => {
    const fromItems = Object.values(items)
      .filter((x) => citesOf(x).some((r) => r.split(":")[0] === id))
      .map((x) => x.id);
    const fromBrief = allSentences(summary)
      .filter((s) => s.derived_from.includes(id))
      .map((s) => s.id);
    return { items: fromItems, sentences: fromBrief };
  }, [items, summary, id]);
  if (!it) return <p className="p-3.5 text-desk-muted italic">{id} (missing)</p>;

  return (
    <>
      <div className="border-b border-desk-line px-3.5 py-3">
        <div className="flex items-center gap-2">
          <span className="border border-desk-ink">
            <KindBadge kind={it.kind} />
          </span>
          <span className="text-white">{it.id}</span>
          <span className="text-desk-muted">{human(it.category)}</span>
          <span className="ml-auto">
            <StatusText status={it.verification.status} />
          </span>
        </div>
        <div className="mt-2 font-serif text-[16px] text-white">{itemText(it)}</div>
        {it.kind === "data" && (
          <div className="mt-1 text-desk-muted">
            {formatValue(it.value)} · as reported “{it.value?.as_reported}” · {it.value?.period} · extracted by {it.extracted_by}
          </div>
        )}
        {it.kind === "extrapolation" && (
          <div className="mt-1 text-desk-muted">
            {human(it.criterion)} · score {it.score === null ? `N/A (${it.na_reason})` : it.score} · confidence {it.confidence}
            {it.rule.type === "threshold" && ` · +1 if ${it.rule.plus_one}, −1 if ${it.rule.minus_one}`}
          </div>
        )}
      </div>

      <CertaintyBlock c={it.certainty} />

      {it.kind === "data" && (
        <Block title="Evidence">
          {it.citations.map((c, i) => (
            <div key={i} className="mb-2">
              <div className="text-desk-muted">
                {c.role.replace("_", " ")} · {c.doc_id} · {c.locator.type === "text" ? `page ${c.locator.page}, chars ${c.locator.char_start}–${c.locator.char_end}` : c.locator.pointer}
              </div>
              <div className="border-l-2 border-desk-muted px-2 py-1 font-serif text-[14px] text-white">“{c.quote}”</div>
              {c.locator.type === "text" && (
                <button onClick={() => openWin("page", `${c.doc_id}|${c.locator.type === "text" ? c.locator.page : 1}|${it.id}`)} className="mt-1 text-desk-link hover:underline">
                  Open the source page
                </button>
              )}
            </div>
          ))}
        </Block>
      )}

      {it.kind === "conclusion" && it.computations.length > 0 && (
        <Block title="Computations">
          {it.computations.map((c) => (
            <div key={c.name} className="mb-1">
              <span className="text-white">{c.name}</span> = {c.expr} → <span className="text-white">{c.display}</span>
            </div>
          ))}
        </Block>
      )}

      <Block title="Citation chain">
        <div>
          <span className="text-desk-muted">Cites: </span>
          {citesOf(it).length ? citesOf(it).map((r, i) => <span key={r}>{i > 0 && ", "}<Ref id={r} /></span>) : "—"}
        </div>
        <div className="mt-1">
          <span className="text-desk-muted">Cited by: </span>
          {[...citedBy.items, ...citedBy.sentences].length
            ? [...citedBy.items, ...citedBy.sentences].map((r, i) => (
                <span key={r}>
                  {i > 0 && ", "}
                  {r.startsWith("H-") ? <SentenceRef id={r} /> : <Ref id={r} />}
                </span>
              ))
            : "—"}
        </div>
      </Block>

      <Block title="Checks">
        <Checks checks={it.verification.checks} />
      </Block>

      {it.verification.attempts.length > 0 && (
        <Block title="Repair history">
          {it.verification.attempts.map((a) => (
            <div key={a.attempt} className="mb-3">
              <div>
                Attempt {a.attempt} · <StatusText status={a.status} />
                {a.action && <span className="text-desk-muted"> · then {a.action.replace("_", " ")}</span>}
              </div>
              {a.before && a.after && a.before !== a.after && (
                <div className="mt-1">
                  <div className="text-desk-bad line-through">{a.before}</div>
                  <div className="text-desk-good">{a.after}</div>
                </div>
              )}
              <Checks checks={a.checks.filter((c) => c.result !== "pass")} />
              {a.note && <div className="text-desk-muted">{a.note}</div>}
            </div>
          ))}
        </Block>
      )}

      <CommentBox targetType="item" targetId={it.id} runId={runId} />
    </>
  );
}

function SentenceRef({ id }: { id: string }) {
  const select = useRunStore((s) => s.select);
  return (
    <button onClick={() => select({ type: "sentence", id })} className="text-desk-link hover:underline">
      {id}
    </button>
  );
}

function SentenceInspector({ id }: { id: string }) {
  const summary = useRunStore((s) => s.summary);
  const items = useRunStore((s) => s.items);
  const runId = useRunStore((s) => s.runId)!;
  const s = allSentences(summary).find((x) => x.id === id);
  if (!s) return <p className="p-3.5 text-desk-muted italic">{id} (missing)</p>;
  return (
    <>
      <div className="border-b border-desk-line px-3.5 py-3">
        <div className="flex items-center gap-2">
          <span className="text-white">{s.id}</span>
          <span className="text-desk-muted">{s.mode === "copied" ? "copied from the agent report" : "summarized"}</span>
          <span className="ml-auto">
            <StatusText status={s.verification.status} />
          </span>
        </div>
        <div className="mt-2 font-serif text-[16px] text-white">{s.text}</div>
      </div>
      <CertaintyBlock c={s.certainty} />
      <Block title="Derived from">
        <ChainTree refs={s.derived_from} items={items} depth={0} />
      </Block>
      <Block title="Checks">
        <Checks checks={s.verification.checks} />
      </Block>
      <CommentBox targetType="summary_sentence" targetId={s.id} runId={runId} />
    </>
  );
}

// Walks brief sentence → conclusions/scores → data items → source quote.
export function ChainTree({ refs, items, depth }: { refs: string[]; items: Record<string, Item>; depth: number }) {
  const select = useRunStore((s) => s.select);
  return (
    <ul className={depth ? "ml-3 border-l border-desk-line-strong pl-2.5" : ""}>
      {refs.map((r) => {
        const base = r.split(":")[0];
        const it = items[base];
        if (!it || r === "rollup" || r.startsWith("HOLE-") || r.startsWith("S-"))
          return (
            <li key={r} className="py-0.5">
              <Ref id={r} />
            </li>
          );
        const kids = it.kind === "data" ? [] : citesOf(it);
        return (
          <li key={r} className="py-0.5">
            <button onClick={() => select({ type: "item", id: base })} className="text-left hover:text-white">
              <span className="text-desk-link">{r}</span> <CertaintyPill c={it.certainty} />{" "}
              <span className="text-desk-muted">{itemText(it).slice(0, 90)}{itemText(it).length > 90 ? "…" : ""}</span>
            </button>
            {it.kind === "data" && it.citations[0] && <div className="ml-3 text-desk-ink">“{it.citations[0].quote}”</div>}
            {kids.length > 0 && depth < 3 && <ChainTree refs={kids.slice(0, 12)} items={items} depth={depth + 1} />}
          </li>
        );
      })}
    </ul>
  );
}

function SourceInspector({ id }: { id: string }) {
  const s = useRunStore((x) => x.sources[id]);
  const runId = useRunStore((x) => x.runId)!;
  const traffic = useRunStore((x) => x.traffic);
  if (!s) return <p className="p-3.5 text-desk-muted italic">{id} (missing)</p>;
  const reqs = traffic.filter((t) => t.data.source_id === id);
  return (
    <>
      <div className="border-b border-desk-line px-3.5 py-3">
        <div className="flex items-center gap-2">
          <span className="text-white">{s.source_id}</span>
          <span className="text-desk-muted">{s.tier.replace("_", " ")} · rank {s.rank}</span>
          <span className="ml-auto">
            <SourceChip status={s.status} />
          </span>
        </div>
        <div className="mt-2 font-serif text-[16px] text-white">{s.title}</div>
        <a href={s.url} target="_blank" rel="noreferrer" className="break-all text-desk-link hover:underline">
          {s.url}
        </a>
      </div>
      <Block title="Why this source">
        <div>{s.rationale}</div>
        <div className="mt-1 text-desk-muted">
          reliability {s.scores.reliability.toFixed(2)} · relevance {s.scores.relevance.toFixed(2)} · approval {s.approval.replace("_", " ")} · {s.access.method}
          {s.access.auth === "api_key" ? " · API key held server-side" : ""}
        </div>
        <div className="mt-1 text-desk-muted">Criteria: {s.criteria.map(human).join(", ") || "—"}</div>
      </Block>
      <Block title="Collected">
        <div>
          {s.stats.pages} pages · {s.stats.bytes.toLocaleString()} bytes · {s.stats.data_items_verified}/{s.stats.data_items} items verified
        </div>
        <div className="text-desk-muted">Docs: {s.doc_ids.join(", ") || "—"}</div>
        {s.error && <div className="text-desk-bad">{s.error}</div>}
      </Block>
      <Block title={`Gateway requests (${reqs.length})`}>
        {reqs.slice(-12).map((t) => (
          <div key={t.seq} className={t.data.decision === "blocked" ? "text-desk-bad" : t.data.decision === "flagged" ? "text-desk-warn" : ""}>
            {t.data.decision.toUpperCase()} · {t.data.reason}
          </div>
        ))}
        {!reqs.length && <span className="text-desk-muted">None yet.</span>}
      </Block>
      <CommentBox targetType="source" targetId={s.source_id} runId={runId} />
    </>
  );
}

function CommentBox({ targetType, targetId, runId }: { targetType: "item" | "source" | "summary_sentence"; targetId: string; runId: string }) {
  const comments = useRunStore((s) => s.comments);
  const status = useRunStore((s) => s.run?.status);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const thread = Object.values(comments).filter((c) => c.target_id === targetId);
  return (
    <Block title="Comment to the agents">
      {thread.map((c) => (
        <div key={c.comment_id} className="mb-2">
          <div className="text-white">“{c.text}”</div>
          <div className={c.status === "applied" ? "text-desk-good" : c.status === "rejected" ? "text-desk-bad" : "animate-pulse text-accent"}>
            {c.status.toUpperCase()}
            {c.response ? ` · Bobby: ${c.response}` : ""}
            {c.changed_item_ids.length ? ` · changed ${c.changed_item_ids.join(", ")}` : ""}
          </div>
        </div>
      ))}
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          if (!text.trim()) return;
          setErr(null);
          try {
            const c = await api.comment(runId, { target_type: targetType, target_id: targetId, text: text.trim() });
            useRunStore.setState((s) => ({ comments: { ...s.comments, [c.comment_id]: c } }));
            setText("");
          } catch (x) {
            setErr(x instanceof Error ? x.message : String(x));
          }
        }}
      >
        <textarea
          aria-label="Comment"
          rows={2}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={status === "stopped"}
          placeholder={targetType === "item" ? "Tell the agents what to change" : "Note for the agents"}
          className="w-full resize-y rounded-[4px] border border-desk-line bg-desk-raised px-2 py-1.5 text-desk-ink placeholder:text-desk-muted focus:border-desk-muted"
        />
        <button disabled={!text.trim() || status === "stopped"} className="mt-1.5 h-7 rounded-[4px] bg-desk-ink px-3 text-[12px] font-medium text-desk disabled:opacity-40">
          Send
        </button>
        {err && <div className="text-desk-bad">{err}</div>}
      </form>
    </Block>
  );
}
