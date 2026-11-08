"use client";

// ⌘K: search items, brief sentences, sources, agents and holes, and open the
// result in a window. Enter opens it, ⌘Enter (Ctrl+Enter) opens it beside the focused window.
import { useEffect, useMemo, useRef, useState } from "react";
import { allSentences, useRunStore } from "@/lib/store";
import { useWindows } from "@/lib/windows";
import { itemText } from "./report";

type Kind = "item" | "sentence" | "source" | "agent" | "hole";
interface Result {
  kind: Kind;
  id: string;
  text: string;
  score: number;
}
const KIND_LABEL: Record<Kind, string> = { item: "Item", sentence: "Sentence", source: "Source", agent: "Agent", hole: "Hole" };
const KIND_RANK: Kind[] = ["item", "agent", "source", "sentence", "hole"];

// Lower is better: exact id, id prefix, id substring, then text substring.
function score(q: string, id: string, text: string): number {
  if (!q) return 4;
  const i = id.toLowerCase();
  if (i === q) return 0;
  if (i.startsWith(q)) return 1;
  if (i.includes(q)) return 2;
  if (text.toLowerCase().includes(q)) return 3;
  return -1;
}

export function Palette() {
  const open = useWindows((s) => s.palette);
  return open ? <PaletteDialog /> : null;
}

function PaletteDialog() {
  const [q, setQ] = useState("");
  const [at, setAt] = useState(0);
  const items = useRunStore((s) => s.items);
  const summary = useRunStore((s) => s.summary);
  const sources = useRunStore((s) => s.sources);
  const agents = useRunStore((s) => s.agents);
  const holes = useRunStore((s) => s.report?.holes);
  const listRef = useRef<HTMLUListElement | null>(null);
  const close = () => useWindows.setState({ palette: false });

  // Give focus back to wherever it was when the palette closes.
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    return () => prev?.focus?.();
  }, []);

  const results = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const out: Result[] = [];
    const add = (kind: Kind, id: string, text: string) => {
      const s = score(needle, id, text);
      if (s >= 0) out.push({ kind, id, text, score: s });
    };
    for (const it of Object.values(items)) add("item", it.id, itemText(it));
    for (const s of allSentences(summary)) add("sentence", s.id, s.text);
    for (const s of Object.values(sources)) add("source", s.source_id, `${s.title} ${s.domain}`);
    for (const a of Object.values(agents)) add("agent", a.agent_id, `${a.role}: ${a.task}`);
    for (const h of holes ?? []) add("hole", h.hole_id, h.reason);
    return out.sort((a, b) => a.score - b.score || KIND_RANK.indexOf(a.kind) - KIND_RANK.indexOf(b.kind) || a.id.localeCompare(b.id, undefined, { numeric: true })).slice(0, 50);
  }, [q, items, summary, sources, agents, holes]);

  useEffect(() => setAt(0), [q]);
  useEffect(() => {
    listRef.current?.querySelector(`[data-i="${at}"]`)?.scrollIntoView({ block: "nearest" });
  }, [at]);

  const choose = (r: Result | undefined, beside: boolean) => {
    if (!r) return;
    const { open } = useWindows.getState();
    const { select } = useRunStore.getState();
    close();
    if (r.kind === "agent") return open("agent", r.id, { beside });
    if (r.kind === "item") select({ type: "item", id: r.id });
    if (r.kind === "sentence") select({ type: "sentence", id: r.id });
    const hole = r.kind === "hole" ? holes?.find((h) => h.hole_id === r.id) : null;
    open("item", hole?.item_id ? `item:${hole.item_id}` : `${r.kind}:${r.id}`, { beside });
  };

  return (
    <div className="fixed inset-0 z-[200000] flex items-start justify-center bg-black/40 px-4 pt-[12vh] print:hidden" onMouseDown={close}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Open anything"
        className="flex max-h-[70vh] w-full max-w-[640px] flex-col border border-desk-line-strong bg-desk text-[13px] text-desk-ink"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <label htmlFor="palette-q" className="sr-only">
          Search items, final report sentences, sources, agents and holes
        </label>
        <input
          id="palette-q"
          autoFocus
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-list"
          aria-activedescendant={results[at] ? `palette-${at}` : undefined}
          aria-autocomplete="list"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              close();
            } else if (e.key === "ArrowDown") {
              e.preventDefault();
              setAt((i) => Math.min(i + 1, results.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setAt((i) => Math.max(i - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              choose(results[at], e.metaKey || e.ctrlKey);
            }
          }}
          placeholder="Search ids and text"
          className="h-11 flex-none border-b border-desk-line bg-transparent px-4 text-[14px] text-white outline-none placeholder:text-desk-muted"
        />
        <ul id="palette-list" role="listbox" aria-label="Results" ref={listRef} className="min-h-0 flex-1 overflow-y-auto py-1">
          {results.map((r, i) => (
            <li
              key={`${r.kind}:${r.id}`}
              id={`palette-${i}`}
              data-i={i}
              role="option"
              aria-selected={i === at}
              onMouseMove={() => setAt(i)}
              onClick={(e) => choose(r, e.metaKey || e.ctrlKey)}
              className={`flex min-h-8 cursor-pointer items-baseline gap-3 px-4 py-1.5 ${i === at ? "bg-desk-raised" : ""}`}
            >
              <span className="w-16 flex-none text-[11px] text-desk-muted">{KIND_LABEL[r.kind]}</span>
              <span className="w-28 flex-none truncate font-mono text-white">{r.id}</span>
              <span className="min-w-0 flex-1 truncate text-desk-muted">{r.text}</span>
            </li>
          ))}
          {results.length === 0 && <li className="px-4 py-3 text-desk-muted">Nothing matches.</li>}
        </ul>
        <div className="flex-none border-t border-desk-line px-4 py-1.5 text-[11px] text-desk-muted">
          ↵ open   ⌘↵ open beside   esc close
        </div>
      </div>
    </div>
  );
}
