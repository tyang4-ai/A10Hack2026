"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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

// How the source window shows a page: the stored PDF page as an image, or the text the checker matched.
type View = "page" | "text";
const VIEW_KEY = "axelrod.sourcePage.view.v1";

function readView(): View | null {
  try {
    const v = localStorage.getItem(VIEW_KEY);
    return v === "page" || v === "text" ? v : null;
  } catch {
    return null;
  }
}

function writeView(v: View) {
  try {
    localStorage.setItem(VIEW_KEY, v);
  } catch {
    // Storage blocked: the choice just won't survive a reload.
  }
}

// Documents whose page image already failed in this tab ("run|doc"), so reopening one goes straight to text.
const NO_PDF = new Set<string>();

// Forget what this tab learned about a run's pages, for when the run is deleted.
export function forgetRunPages(runId: string) {
  for (const k of [...NO_PDF]) if (k.startsWith(`${runId}|`)) NO_PDF.delete(k);
}

// The stored page, as the real PDF page with the quotes highlighted or as the stored text.
// The text view is what the checker matched, with cited spans highlighted. unit_context spans are lighter.
export function DocPage({ runId, docId, startPage, itemId }: { runId: string; docId: string; startPage: number; itemId: string }) {
  const [page, setPage] = useState(startPage);
  const [data, setData] = useState<DocumentPage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Kept across page turns, so the view doesn't flicker while the next page loads.
  const [contentType, setContentType] = useState<DocumentPage["content_type"] | null>(null);
  const [saved, setSaved] = useState<View | null>(null);
  const [picked, setPicked] = useState<View | null>(null);
  const pdfKey = `${runId}|${docId}`;
  const [noPdf, setNoPdf] = useState(() => NO_PDF.has(pdfKey));
  const mark = useRef<HTMLElement | null>(null);
  useEffect(() => setSaved(readView()), []);
  useEffect(() => {
    setData(null);
    setErr(null);
    api.page(runId, docId, page).then(
      (d) => {
        setData(d);
        setContentType(d.content_type);
      },
      (e) => setErr(e instanceof Error ? e.message : String(e)),
    );
  }, [runId, docId, page]);

  // A click in this window wins. Otherwise a PDF opens as the saved choice (Page by default), anything else as text.
  const wanted: View | null = picked ?? (contentType === null ? null : contentType === "pdf" ? (saved ?? "page") : "text");
  const fellBack = wanted === "page" && noPdf;
  const view: View | null = fellBack ? "text" : wanted;

  useEffect(() => {
    if (view !== "page") void mark.current?.scrollIntoView({ block: "center" });
  }, [data, view]);

  const choose = (v: View) => {
    setPicked(v);
    writeView(v);
    if (v === "page") {
      NO_PDF.delete(pdfKey);
      setNoPdf(false);
    }
  };
  const onImageFail = useCallback(() => {
    NO_PDF.add(pdfKey);
    setNoPdf(true);
  }, [pdfKey]);

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
        <ViewToggle view={view} onChoose={choose} />
        <span className="ml-auto text-muted">{view === "page" ? "Stored PDF page" : "Stored source text"}</span>
      </div>
      {fellBack && (
        <p role="status" className="flex-none border-b border-rule px-4 py-1.5 text-[12px] text-muted">
          No stored PDF for this document. Showing the text the checker matched.
        </p>
      )}
      {view === "page" ? (
        <PageImage
          key={`${page}|${itemId}`}
          src={api.pageImageUrl(runId, docId, page, itemId || undefined)}
          docId={docId}
          page={page}
          itemId={itemId}
          onFail={onImageFail}
        />
      ) : (
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
      )}
      {view === "page" ? (
        <div className="flex flex-none flex-wrap items-center gap-x-3 border-t border-rule px-4 py-1.5 text-[11px] text-muted">
          <span>{itemId || "Cited"} quotes:</span>
          <Swatch className="bg-good" label="pass" />
          <Swatch className="bg-warn" label="fixed" />
          <Swatch className="bg-bad" label="fail" />
        </div>
      ) : (
        <div className="flex-none border-t border-rule px-4 py-1.5 text-[11px] text-muted">
          <mark className="bg-hl-evidence px-1">{itemId || "item"} evidence</mark> <mark className="bg-hl-context px-1">unit or period context</mark>{" "}
          <mark className="bg-hl-other px-1">other cited items</mark>
        </div>
      )}
    </div>
  );
}

// Page shows the stored PDF page, Text the stored text. Both keep the page navigation.
function ViewToggle({ view, onChoose }: { view: View | null; onChoose: (v: View) => void }) {
  const opts: [View, string, string][] = [
    ["page", "Page", "The stored PDF page, with the quotes highlighted"],
    ["text", "Text", "The stored text the checker matched"],
  ];
  return (
    <span role="group" aria-label="Show the page as" className="ml-2 inline-flex overflow-hidden rounded-[4px] border border-control">
      {opts.map(([v, label, help]) => (
        <button
          key={v}
          type="button"
          title={help}
          aria-pressed={view === v}
          onClick={() => onChoose(v)}
          className={`h-6 px-2 text-[12px] ${view === v ? "bg-ink text-paper" : "bg-paper text-ink hover:bg-hover"}`}
        >
          {label}
        </button>
      ))}
    </span>
  );
}

function Swatch({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span aria-hidden className={`inline-block h-2.5 w-2.5 rounded-[2px] opacity-60 ${className}`} />
      {label}
    </span>
  );
}

// The page image, scaled to the panel width. Tall pages scroll inside the panel.
// Any load failure (404 when no PDF is stored) hands back to the text view.
function PageImage({ src, docId, page, itemId, onFail }: { src: string; docId: string; page: number; itemId: string; onFail: () => void }) {
  const [loaded, setLoaded] = useState(false);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-auto bg-zebra p-2">
        {!loaded && (
          <p aria-busy="true" className="px-3 py-2 font-mono text-[13px] text-muted">
            Loading the page image
          </p>
        )}
        {/* A plain img: the API makes the PNG on demand, so there is nothing for next/image to optimize. */}
        <img
          src={src}
          alt={`${docId} page ${page}${itemId ? `, with ${itemId}'s quotes highlighted` : ""}`}
          onLoad={() => setLoaded(true)}
          onError={onFail}
          className={`mx-auto block w-full border border-rule bg-white ${loaded ? "h-auto" : "invisible h-0"}`}
        />
      </div>
      {loaded && (
        <div className="flex-none border-t border-rule px-4 py-1 text-[12px]">
          <a href={src} target="_blank" rel="noopener noreferrer" className="text-link hover:underline">
            Open the page image full size
          </a>
        </div>
      )}
    </div>
  );
}
