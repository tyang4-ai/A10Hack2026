"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { DocumentPage, Item } from "@contract/types";
import { api } from "@/lib/api";
import { allSentences, useRunStore } from "@/lib/store";
import { citesOf } from "./Inspector";
import { btnSm } from "./ui";

// A page ref is "doc_id|page|item_id": which stored page to show and whose quote to highlight.
export function parsePageRef(ref: string) {
  const [doc, page, item] = ref.split("|");
  return { doc, page: Number(page) || 1, item: item ?? "" };
}

// Follow an item down its citations to the first quote on a stored text page.
export function pageRefFor(items: Record<string, Item>, id: string, depth = 0): string | null {
  const it = items[id.split(":")[0]];
  if (!it || depth > 4) return null;
  if (it.kind === "data") {
    const c = it.citations.find((x) => x.locator.type === "text");
    return c && c.locator.type === "text" ? `${c.doc_id}|${c.locator.page}|${it.id}` : null;
  }
  for (const r of citesOf(it)) {
    const found = pageRefFor(items, r, depth + 1);
    if (found) return found;
  }
  return null;
}

// The unpinned source page window: it shows the page behind whatever is selected,
// and keeps the last page when the selection has none (an agent, a source).
export function FollowPage() {
  const runId = useRunStore((s) => s.runId);
  const ref = useRunStore((s) => {
    const sel = s.selected;
    if (sel?.type === "item") return pageRefFor(s.items, sel.id);
    if (sel?.type === "sentence") {
      const sen = allSentences(s.summary).find((x) => x.id === sel.id);
      for (const r of sen?.derived_from ?? []) {
        const found = pageRefFor(s.items, r);
        if (found) return found;
      }
    }
    return null;
  });
  const [last, setLast] = useState<string | null>(null);
  useEffect(() => {
    if (ref) setLast(ref);
  }, [ref]);
  useEffect(() => setLast(null), [runId]);
  const selItem = useRunStore((s) => (s.selected?.type === "item" || s.selected?.type === "sentence" ? s.selected.id : null));
  const shown = ref ?? last;
  if (!runId) return null;
  const noPage = selItem && !ref ? `${selItem} cites structured data or the scorecard. There's no page to show.` : null;
  if (!shown) return <p className="p-4 text-[13px] text-muted">{noPage ?? "Select an item to see its source."}</p>;
  const p = parsePageRef(shown);
  return (
    <div className="flex h-full flex-col">
      {noPage && (
        <p role="status" className="flex-none border-b border-rule bg-warn-bg px-4 py-1.5 text-[12px] text-ink">
          {noPage}
        </p>
      )}
      <div className="min-h-0 flex-1">
        <DocPage key={shown} runId={runId} docId={p.doc} startPage={p.page} itemId={p.item} />
      </div>
    </div>
  );
}

// The stored page text with cited spans highlighted. unit_context spans are lighter.
export function DocPage({ runId, docId, startPage, itemId }: { runId: string; docId: string; startPage: number; itemId: string }) {
  const [page, setPage] = useState(startPage);
  const [data, setData] = useState<DocumentPage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const mark = useRef<HTMLElement | null>(null);
  useEffect(() => {
    setData(null);
    setErr(null);
    api.page(runId, docId, page).then(setData, (e) => setErr(e instanceof Error ? e.message : String(e)));
  }, [runId, docId, page]);
  useEffect(() => {
    void mark.current?.scrollIntoView({ block: "center" });
  }, [data]);

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
    <div className="flex h-full flex-col text-ink">
      <div className="flex flex-none flex-wrap items-center gap-2 border-b border-rule px-4 py-1.5 text-[13px]">
        <span className="font-mono font-semibold">{docId}</span>
        <span className="font-mono tabular-nums">
          page {page}
          {data ? ` of ${data.pages}` : ""}
        </span>
        <button className={`${btnSm} w-7 px-0`} aria-label="Previous page" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
          ◂
        </button>
        <button className={`${btnSm} w-7 px-0`} aria-label="Next page" disabled={!data || page >= data.pages} onClick={() => setPage((p) => p + 1)}>
          ▸
        </button>
        <span className="ml-auto text-muted">Stored source text</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-5 py-4 font-mono text-[13px] leading-relaxed whitespace-pre-wrap">
        {err && (
          <span role="alert" className="text-bad">
            {err}
          </span>
        )}
        {!data && !err && (
          <span aria-busy="true" className="text-muted">
            Loading the page
          </span>
        )}
        {segments.map((s, i) => {
          if (!s.h) return <span key={i}>{s.text}</span>;
          const mine = s.h.item_id === itemId;
          const ctx = s.h.role !== "evidence";
          const cls = mine ? (ctx ? "bg-hl-context" : "bg-hl-evidence outline-2 outline-ink") : ctx ? "bg-hl-other-context" : "bg-hl-other";
          return (
            <mark
              key={i}
              ref={
                mine && !ctx
                  ? (el) => {
                      mark.current = el;
                    }
                  : undefined
              }
              title={`${s.h.item_id}: ${s.h.role}, ${s.h.status}`}
              className={`${cls} text-ink`}
            >
              {s.text}
            </mark>
          );
        })}
      </div>
      <div className="flex-none border-t border-rule px-4 py-1.5 text-[11px] text-muted">
        <mark className="bg-hl-evidence px-1">{itemId || "item"} evidence</mark> <mark className="bg-hl-context px-1">unit or period context</mark>{" "}
        <mark className="bg-hl-other px-1">other cited items</mark>
      </div>
    </div>
  );
}
