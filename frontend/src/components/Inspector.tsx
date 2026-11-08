"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { Certainty, CheckResult, Citation, DiffSegment, DocumentPage, Item } from "@contract/types";
import { api } from "@/lib/api";
import { CERTAINTY_HELP, formatValue, human } from "@/lib/format";
import { allSentences, useRunStore } from "@/lib/store";
import { itemText } from "./report";
import { btn, CertaintyPill, KindBadge, Label, SourceChip, StatusText } from "./ui";

export function Inspector() {
  const selected = useRunStore((s) => s.selected);
  return (
    <aside aria-label="Inspector" className="flex h-full flex-col bg-desk font-mono text-xs text-desk-ink">
      <div className="flex flex-none items-center justify-between border-b border-desk-line px-3.5 py-2.5">
        <span className="font-bold tracking-[0.14em]">INSPECTOR</span>
        <span className="text-desk-muted">{selected ? selected.id : ""}</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {!selected && <p className="p-3.5 text-desk-muted">Select a source, report item or brief sentence to see its evidence, checks and certainty.</p>}
        {selected?.type === "item" && <ItemInspector id={selected.id} />}
        {selected?.type === "source" && <SourceInspector id={selected.id} />}
        {selected?.type === "sentence" && <SentenceInspector id={selected.id} />}
      </div>
    </aside>
  );
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-desk-line px-3.5 py-3">
      <div className="mb-1.5 text-[10px] tracking-[0.12em] text-desk-muted">{title}</div>
      {children}
    </div>
  );
}

function Ref({ id }: { id: string }) {
  const items = useRunStore((s) => s.items);
  const select = useRunStore((s) => s.select);
  if (id === "rollup") return <span className="text-desk-muted">rollup (scorecard)</span>;
  if (id.startsWith("HOLE-")) return <span className="text-amber">{id} (declared gap)</span>;
  if (id.startsWith("S-")) return <button onClick={() => select({ type: "source", id })} className="text-[#6CB6FF] hover:underline">{id}</button>;
  const base = id.split(":")[0];
  const it = items[base];
  if (!it) return <span className="text-desk-muted italic">{id} (missing)</span>;
  return (
    <button onClick={() => select({ type: "item", id: base })} className="text-left text-[#6CB6FF] hover:underline">
      {id}
    </button>
  );
}

export function CertaintyBlock({ c }: { c: Certainty | null }) {
  if (!c) return null;
  return (
    <Block title="CERTAINTY">
      <div className="flex items-baseline gap-3" title={CERTAINTY_HELP}>
        <span className="text-[26px] text-white">{c.score ?? "—"}</span>
        <span className="uppercase">{c.band}</span>
      </div>
      <div className="mt-1 tabular-nums">
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
          <span className={`font-bold ${c.result === "pass" ? "text-desk-good" : c.result === "fail" ? "text-desk-bad" : "text-amber"}`}>
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

function citesOf(it: Item): string[] {
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
  const [doc, setDoc] = useState<Citation | null>(null);
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
        <Block title="EVIDENCE">
          {it.citations.map((c, i) => (
            <div key={i} className="mb-2">
              <div className="text-desk-muted">
                {c.role.replace("_", " ")} · {c.doc_id} · {c.locator.type === "text" ? `page ${c.locator.page}, chars ${c.locator.char_start}–${c.locator.char_end}` : c.locator.pointer}
              </div>
              <div className="border-l-2 border-[#555] px-2 py-1 text-white">“{c.quote}”</div>
              {c.locator.type === "text" && (
                <button onClick={() => setDoc(c)} className="mt-1 text-[#6CB6FF] hover:underline">
                  OPEN SOURCE ▸
                </button>
              )}
            </div>
          ))}
        </Block>
      )}

      {it.kind === "conclusion" && it.computations.length > 0 && (
        <Block title="COMPUTATIONS">
          {it.computations.map((c) => (
            <div key={c.name} className="mb-1">
              <span className="text-white">{c.name}</span> = {c.expr} → <span className="text-white">{c.display}</span>
            </div>
          ))}
        </Block>
      )}

      <Block title="CHAIN">
        <div>
          <span className="text-desk-muted">Cites </span>
          {citesOf(it).length ? citesOf(it).map((r, i) => <span key={r}>{i > 0 && ", "}<Ref id={r} /></span>) : "—"}
        </div>
        <div className="mt-1">
          <span className="text-desk-muted">Cited by </span>
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

      <Block title="CHECKS">
        <Checks checks={it.verification.checks} />
      </Block>

      {it.verification.attempts.length > 0 && (
        <Block title="REPAIR HISTORY">
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
      {doc && <DocViewer runId={runId} citation={doc} itemId={it.id} onClose={() => setDoc(null)} />}
    </>
  );
}

function SentenceRef({ id }: { id: string }) {
  const select = useRunStore((s) => s.select);
  return (
    <button onClick={() => select({ type: "sentence", id })} className="text-[#6CB6FF] hover:underline">
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
          <span className="text-desk-muted">{s.mode === "copied" ? "copied from the report" : "summarized"}</span>
          <span className="ml-auto">
            <StatusText status={s.verification.status} />
          </span>
        </div>
        <div className="mt-2 font-serif text-[16px] text-white">{s.text}</div>
      </div>
      <CertaintyBlock c={s.certainty} />
      <Block title="DERIVED FROM · FOLLOW IT DOWN">
        <ChainTree refs={s.derived_from} items={items} depth={0} />
      </Block>
      <Block title="CHECKS">
        <Checks checks={s.verification.checks} />
      </Block>
      <CommentBox targetType="summary_sentence" targetId={s.id} runId={runId} />
    </>
  );
}

// Walks brief sentence → conclusions/scores → data items → source quote.
function ChainTree({ refs, items, depth }: { refs: string[]; items: Record<string, Item>; depth: number }) {
  const select = useRunStore((s) => s.select);
  return (
    <ul className={depth ? "ml-3 border-l border-[#3A3A3A] pl-2.5" : ""}>
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
              <span className="text-[#6CB6FF]">{r}</span> <CertaintyPill c={it.certainty} />{" "}
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
        <a href={s.url} target="_blank" rel="noreferrer" className="break-all text-[#6CB6FF] hover:underline">
          {s.url}
        </a>
      </div>
      <Block title="WHY THIS SOURCE">
        <div>{s.rationale}</div>
        <div className="mt-1 text-desk-muted">
          reliability {s.scores.reliability.toFixed(2)} · relevance {s.scores.relevance.toFixed(2)} · approval {s.approval.replace("_", " ")} · {s.access.method}
          {s.access.auth === "api_key" ? " · API key held server-side" : ""}
        </div>
        <div className="mt-1 text-desk-muted">Criteria: {s.criteria.map(human).join(", ") || "—"}</div>
      </Block>
      <Block title="COLLECTED">
        <div>
          {s.stats.pages} pages · {s.stats.bytes.toLocaleString()} bytes · {s.stats.data_items_verified}/{s.stats.data_items} items verified
        </div>
        <div className="text-desk-muted">Docs: {s.doc_ids.join(", ") || "—"}</div>
        {s.error && <div className="text-desk-bad">{s.error}</div>}
      </Block>
      <Block title={`GATEWAY REQUESTS · ${reqs.length}`}>
        {reqs.slice(-12).map((t) => (
          <div key={t.seq} className={t.data.decision === "blocked" ? "text-desk-bad" : t.data.decision === "flagged" ? "text-amber" : ""}>
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
    <Block title="COMMENT TO THE AGENTS">
      {thread.map((c) => (
        <div key={c.comment_id} className="mb-2">
          <div className="text-white">“{c.text}”</div>
          <div className={c.status === "applied" ? "text-desk-good" : c.status === "rejected" ? "text-desk-bad" : "animate-pulse text-amber"}>
            {c.status.toUpperCase()}
            {c.response ? ` · ${c.response}` : ""}
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
          placeholder={targetType === "item" ? "Tell the agents what to change. The item is revised and re-checked." : "Note for the agents"}
          className="w-full resize-y border border-[#555] bg-transparent px-2 py-1 text-desk-ink outline-none placeholder:text-desk-muted focus:border-desk-ink"
        />
        <button disabled={!text.trim() || status === "stopped"} className="mt-1 border border-desk-ink px-2 py-0.5 disabled:opacity-40">
          SEND
        </button>
        {err && <div className="text-desk-bad">{err}</div>}
      </form>
    </Block>
  );
}

// The stored page text with cited spans highlighted. unit_context spans are lighter.
function DocViewer({ runId, citation, itemId, onClose }: { runId: string; citation: Citation; itemId: string; onClose: () => void }) {
  const loc = citation.locator.type === "text" ? citation.locator : null;
  const [page, setPage] = useState(loc?.page ?? 1);
  const [data, setData] = useState<DocumentPage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const mark = useRef<HTMLElement | null>(null);
  useEffect(() => {
    setData(null);
    api.page(runId, citation.doc_id, page).then(setData, (e) => setErr(String(e)));
  }, [runId, citation.doc_id, page]);
  useEffect(() => {
    mark.current?.scrollIntoView({ block: "center" });
  }, [data]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);

  const segments = useMemo(() => {
    if (!data) return [];
    const hs = [...data.highlights].sort((a, b) => a.char_start - b.char_start || (a.role === "evidence" ? -1 : 1));
    const out: { text: string; h: (typeof hs)[number] | null }[] = [];
    let at = 0;
    for (const h of hs) {
      if (h.char_start < at) continue;
      if (h.char_start > at) out.push({ text: data.text.slice(at, h.char_start), h: null });
      out.push({ text: data.text.slice(h.char_start, h.char_end), h });
      at = h.char_end;
    }
    out.push({ text: data.text.slice(at), h: null });
    return out;
  }, [data]);

  return (
    <div role="dialog" aria-label={`${citation.doc_id} page ${page}`} className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-6" onClick={onClose}>
      <div className="flex max-h-full w-full max-w-3xl flex-col bg-white text-ink" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 border-b border-ink px-4 py-2 font-mono text-xs">
          <span className="font-bold">{citation.doc_id}</span>
          <span>
            page {page}
            {data ? ` of ${data.pages}` : ""} · {data?.content_type}
          </span>
          <button className={btn} disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            ◂
          </button>
          <button className={btn} disabled={!data || page >= data.pages} onClick={() => setPage((p) => p + 1)}>
            ▸
          </button>
          <span className="ml-auto text-muted">Exactly the text the checker matched against</span>
          <button className={btn} onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-6 py-4 font-mono text-[13px] leading-relaxed whitespace-pre-wrap">
          {err && <span className="text-bad">{err}</span>}
          {!data && !err && <span className="text-muted">Loading…</span>}
          {segments.map((s, i) => {
            if (!s.h) return <span key={i}>{s.text}</span>;
            const mine = s.h.item_id === itemId;
            const ctx = s.h.role !== "evidence";
            const cls = mine ? (ctx ? "bg-[#FFF2B8]" : "bg-[#FFD54A] outline-2 outline-ink") : ctx ? "bg-[#F1F1F1]" : "bg-[#E4E4E4]";
            return (
              <mark key={i} ref={mine && !ctx ? (el) => { mark.current = el; } : undefined} title={`${s.h.item_id} · ${s.h.role} · ${s.h.status}`} className={`${cls} text-ink`}>
                {s.text}
              </mark>
            );
          })}
        </div>
        <div className="border-t border-rule px-4 py-1.5 font-mono text-[10px] text-muted">
          <mark className="bg-[#FFD54A] px-1">{itemId} evidence</mark> <mark className="bg-[#FFF2B8] px-1">unit or period context</mark>{" "}
          <mark className="bg-[#E4E4E4] px-1">other cited items</mark> · Esc to close
        </div>
      </div>
    </div>
  );
}
