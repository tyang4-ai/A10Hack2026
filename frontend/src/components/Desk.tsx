"use client";

// The desk that holds every window, the dock along the bottom, and the layout
// control in the top bar.
import { useEffect, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { api } from "@/lib/api";
import { useRunStore } from "@/lib/store";
import { LAYOUTS, useWindows, type Win } from "@/lib/windows";
import { Window } from "./Window";
import { KIND_META, KIND_ORDER, winTitle } from "./WindowContent";

const byKind = (a: Win, b: Win) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) || a.id.localeCompare(b.id);

// Bring the bottom-most visible window to the front, so repeated presses visit every window.
function cycleFocus() {
  const wins = Object.values(useWindows.getState().wins).filter((w) => !w.min);
  if (!wins.length) return;
  const next = wins.sort((a, b) => a.z - b.z)[0];
  useWindows.getState().focus(next.id);
  document.querySelector<HTMLElement>(`[data-win="${CSS.escape(next.id)}"] [data-titlebar]`)?.focus();
}

export function Desk() {
  // Window ids in a stable order, so Tab order doesn't jump when focus changes z-order.
  const ids = useWindows(useShallow((s) => Object.values(s.wins).sort(byKind).map((w) => w.id)));
  const hydrate = useWindows((s) => s.hydrate);
  const deskRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => hydrate(), [hydrate]);

  useEffect(() => {
    const el = deskRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => useWindows.setState({ desk: { w: e.contentRect.width, h: e.contentRect.height } }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        useWindows.setState((s) => ({ palette: !s.palette }));
      } else if (e.ctrlKey && e.code === "Backquote") {
        e.preventDefault();
        cycleFocus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div ref={deskRef} data-desk className="desk desk-grid relative min-h-0 flex-1 overflow-hidden bg-desk">
      {ids.map((id) => (
        <Window key={id} id={id} />
      ))}
      <SnapPreview />
    </div>
  );
}

function SnapPreview() {
  const snap = useWindows((s) => s.snap);
  if (!snap) return null;
  return (
    <div
      aria-hidden
      style={{ left: `${snap.x * 100}%`, top: `${snap.y * 100}%`, width: `${snap.w * 100}%`, height: `${snap.h * 100}%` }}
      className="pointer-events-none absolute z-[100000] border border-accent bg-accent/15 transition-[left,top,width,height] duration-100 print:hidden"
    />
  );
}

// ---- dock ----

const DOT = { focused: "bg-accent", open: "bg-desk-ink", min: "border border-desk-muted" } as const;

export function Dock() {
  const wins = useWindows(useShallow((s) => Object.values(s.wins).sort(byKind)));
  const focused = useWindows((s) => s.focused);
  const show = useWindows((s) => s.show);
  const [mac, setMac] = useState(false);
  useEffect(() => setMac(/Mac|iPhone|iPad/.test(navigator.platform)), []);
  return (
    <nav aria-label="Dock" className="flex h-12 flex-none items-center gap-1 overflow-x-auto border-t border-desk-line bg-desk px-2 text-[12px] text-desk-ink print:hidden">
      <button
        onClick={() => useWindows.setState({ palette: true })}
        className="flex h-8 flex-none items-center gap-2 rounded-[4px] border border-desk-line-strong px-2.5 text-desk-ink hover:border-desk-muted"
      >
        <kbd className="font-mono text-[11px] text-desk-muted">{mac ? "⌘K" : "Ctrl K"}</kbd>
        Open anything
      </button>
      <span aria-hidden className="mx-1 h-6 w-px flex-none bg-desk-line" />
      {wins.map((w) => {
        const state = w.min ? "min" : focused === w.id ? "focused" : "open";
        return (
          <button
            key={w.id}
            onClick={() => show(w.id)}
            aria-label={`${winTitle(w)}${w.min ? ", minimized" : ""}`}
            aria-current={focused === w.id && !w.min ? "true" : undefined}
            className={`flex h-8 max-w-48 flex-none items-center gap-1.5 rounded-[4px] px-2 hover:bg-desk-raised ${focused === w.id && !w.min ? "bg-desk-raised" : ""} ${w.min ? "text-desk-muted" : ""}`}
          >
            <span aria-hidden className={`h-1.5 w-1.5 flex-none rounded-full ${DOT[state]}`} />
            <span aria-hidden className="font-mono text-[11px] text-desk-muted">
              {KIND_META[w.kind].icon}
            </span>
            <span className={`truncate ${w.kind === "agent" && w.ref ? "font-mono" : ""}`}>{winTitle(w)}</span>
            {w.kind === "traffic" && <BlockedBadge />}
            {w.kind === "agents" && <WorkingBadge />}
          </button>
        );
      })}
      <InjectErrors />
    </nav>
  );
}

function BlockedBadge() {
  const n = useRunStore((s) => s.traffic.reduce((k, t) => k + (t.data.decision === "blocked" ? 1 : 0), 0));
  if (!n) return null;
  return (
    <span title={`${n} blocked requests`} className="rounded-[3px] bg-desk-bad px-1 font-mono text-[11px] font-semibold text-desk tabular-nums">
      {n}
    </span>
  );
}

function WorkingBadge() {
  const n = useRunStore((s) => Object.values(s.agents).filter((a) => a.status === "working").length);
  if (!n) return null;
  return (
    <span title={`${n} agents working`} className="rounded-[3px] bg-desk-link px-1 font-mono text-[11px] font-semibold text-desk tabular-nums">
      {n}
    </span>
  );
}

// Demo control, kept visible and labelled as simulated.
function InjectErrors() {
  const run = useRunStore((s) => s.run);
  const [note, setNote] = useState<string | null>(null);
  if (!run || run.status === "stopped") return null;
  return (
    <div className="ml-auto flex flex-none items-center gap-2 pl-3">
      {note && <span className="max-w-72 truncate text-desk-muted" title={note}>{note}</span>}
      <button
        className="h-8 rounded-[4px] border border-dashed border-desk-warn px-2.5 font-medium text-desk-warn hover:bg-desk-raised"
        title="Corrupts one digit in a few quotes so the checker can catch them. Simulated."
        onClick={async () => {
          try {
            const r = await api.injectErrors(run.run_id, 3);
            setNote(`Corrupted one digit in ${r.item_ids.join(", ")}. Watch the checker catch it.`);
          } catch (e) {
            setNote(e instanceof Error ? e.message : String(e));
          }
        }}
      >
        Inject errors (simulated)
      </button>
    </div>
  );
}

// ---- layout control for the top bar ----

const seg = (on: boolean, first: boolean) =>
  `h-7 px-2.5 ${first ? "" : "border-l border-desk-line-strong"} ${on ? "bg-accent font-semibold text-accent-ink" : "text-desk-ink hover:bg-desk-raised"}`;

export function LayoutControl() {
  const layout = useWindows((s) => s.layout);
  const suggest = useWindows((s) => s.suggest);
  const dirty = useWindows((s) => s.dirty);
  const auto = useWindows((s) => s.auto);
  const customs = useWindows((s) => s.customs);
  const { applyLayout, toggleAuto, saveAs, deleteLayout } = useWindows.getState();
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  useEffect(() => {
    if (!confirmDelete) return;
    const t = setTimeout(() => setConfirmDelete(false), 4000);
    return () => clearTimeout(t);
  }, [confirmDelete]);
  const label = (id: string) => LAYOUTS.find((l) => l.id === id)?.label ?? customs.find((c) => c.id === id)?.label ?? id;
  const custom = customs.find((c) => c.id === layout);
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div role="group" aria-label="Layout presets, one per pipeline phase" className="flex overflow-hidden rounded-[4px] border border-desk-line-strong text-[12px]">
        {LAYOUTS.map((l, i) => (
          <button key={l.id} aria-pressed={layout === l.id} title={l.help} onClick={() => applyLayout(l.id)} className={seg(layout === l.id, i === 0)}>
            {l.label}
          </button>
        ))}
      </div>
      {customs.length > 0 && (
        <div role="group" aria-label="Your saved layouts" className="flex overflow-hidden rounded-[4px] border border-desk-line-strong text-[12px]">
          {customs.map((c, i) => (
            <button key={c.id} aria-pressed={layout === c.id} onClick={() => applyLayout(c.id)} className={`${seg(layout === c.id, i === 0)} max-w-32 truncate`}>
              {c.label}
            </button>
          ))}
        </div>
      )}
      {naming ? (
        <form
          className="flex items-center gap-1"
          onSubmit={(e) => {
            e.preventDefault();
            saveAs(name);
            setName("");
            setNaming(false);
          }}
        >
          <label htmlFor="layout-name" className="sr-only">
            Name for this layout
          </label>
          <input
            id="layout-name"
            autoFocus
            value={name}
            maxLength={24}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setNaming(false)}
            placeholder="Layout name"
            className="h-7 w-32 rounded-[4px] border border-desk-line-strong bg-desk-raised px-2 text-[12px] text-desk-ink placeholder:text-desk-muted focus:border-accent"
          />
          <button className="h-7 rounded-[4px] bg-accent px-2.5 text-[12px] font-semibold text-accent-ink">Save</button>
          <button type="button" onClick={() => setNaming(false)} aria-label="Cancel saving the layout" className="h-7 px-1.5 text-desk-muted hover:text-white">
            ✕
          </button>
        </form>
      ) : (
        <button onClick={() => setNaming(true)} title="Save the current windows as your own layout" className="h-7 rounded-[4px] px-1.5 text-[12px] text-desk-muted hover:text-white">
          Save layout as…
        </button>
      )}
      {custom && (
        <button
          onClick={() => (confirmDelete ? (deleteLayout(custom.id), setConfirmDelete(false)) : setConfirmDelete(true))}
          className={`h-7 rounded-[4px] px-1.5 text-[12px] ${confirmDelete ? "bg-desk-bad font-semibold text-desk" : "text-desk-muted hover:text-desk-bad"}`}
        >
          {confirmDelete ? `Confirm delete ${custom.label}` : `Delete ${custom.label}`}
        </button>
      )}
      <button
        aria-pressed={auto}
        onClick={toggleAuto}
        title="Auto tiling: windows never overlap and re-arrange when one opens, closes or minimizes. Drag a window onto another to swap them. Resizing by hand turns it off."
        className={`h-7 rounded-[4px] border px-2.5 text-[12px] ${auto ? "border-accent bg-accent font-semibold text-accent-ink" : "border-desk-line-strong text-desk-ink hover:bg-desk-raised"}`}
      >
        Auto tile
      </button>
      {dirty && !auto && (
        <button onClick={() => applyLayout(layout, { reset: true })} className="h-7 rounded-[4px] px-1.5 text-[12px] text-desk-muted hover:text-white" title={`Put the ${label(layout)} windows back where they started`}>
          Reset
        </button>
      )}
      {suggest && (
        <button onClick={() => applyLayout(suggest)} className="h-7 rounded-[4px] border border-accent px-2.5 text-[12px] font-medium text-accent hover:bg-desk-raised">
          Switch to {label(suggest)} layout
        </button>
      )}
    </div>
  );
}
