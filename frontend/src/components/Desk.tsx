"use client";

// The desk that holds every window, the dock along the bottom, and the layout
// control in the top bar.
import { useEffect, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { api } from "@/lib/api";
import { useRunStore } from "@/lib/store";
import { LAYOUTS, useWindows, type Geo, type Win } from "@/lib/windows";
import { Window } from "./Window";
import { usePopover } from "./Switchers";
import { KIND_META, KIND_ORDER, winTitle } from "./WindowContent";

// "A, B and C"
const listAnd = (xs: string[]) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

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

// One overlay for every drop target. It fades in, morphs between the small fill square
// and the full outline (grows in, shrinks back out), and fades out when there's nothing
// to drop on, so nothing pops.
function SnapPreview() {
  const snap = useWindows((s) => s.snap);
  const hint = useWindows((s) => s.snapHint);
  const target = snap ?? hint;
  const key = target ? `${target.x},${target.y},${target.w},${target.h},${snap ? 1 : 0}` : "";
  const [shown, setShown] = useState<{ g: Geo; solid: boolean } | null>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (target) {
      // The entrance is a keyframe animation (snap-in), so it plays even when this
      // update lands before the browser's next paint, as pointer events make it do.
      setShown({ g: target, solid: !!snap });
      setVisible(true);
      return;
    }
    setVisible(false);
    const t = setTimeout(() => setShown(null), 160);
    return () => clearTimeout(t);
    // key captures target and snap; the objects themselves change identity on every move.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  if (!shown) return null;
  const { g, solid } = shown;
  return (
    <div
      aria-hidden
      style={{ left: `${g.x * 100}%`, top: `${g.y * 100}%`, width: `${g.w * 100}%`, height: `${g.h * 100}%` }}
      className={`pointer-events-none absolute z-[100000] animate-[snap-in_160ms_ease-out] transition-[left,top,width,height,opacity,transform,background-color] duration-150 ease-out print:hidden ${
        visible ? "scale-100 opacity-100" : "scale-[0.96] opacity-0"
      } ${solid ? "rounded-[2px] border border-accent bg-accent/15" : "rounded-[4px] border border-dashed border-accent bg-accent/10"}`}
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
        title="Changes one digit in 3 quotes."
        onClick={async () => {
          try {
            const r = await api.injectErrors(run.run_id, 3);
            setNote(`Changed ${listAnd(r.item_ids)}.`);
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
  const customs = useWindows((s) => s.customs);
  const { applyLayout } = useWindows.getState();
  const label = (id: string) => LAYOUTS.find((l) => l.id === id)?.label ?? customs.find((c) => c.id === id)?.label ?? id;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div role="group" aria-label="Layout presets, one per pipeline phase" className="flex overflow-hidden rounded-[4px] border border-desk-line-strong text-[12px]">
        {LAYOUTS.map((l, i) => (
          <button key={l.id} aria-pressed={layout === l.id} onClick={() => applyLayout(l.id)} className={seg(layout === l.id, i === 0)}>
            {l.label}
          </button>
        ))}
      </div>
      {customs.length > 0 && (
        <div role="group" aria-label="Your saved layouts" className="flex overflow-hidden rounded-[4px] border border-desk-line-strong text-[12px]">
          {customs.map((c, i) => (
            <SavedLayoutButton key={c.id} id={c.id} label={c.label} on={layout === c.id} first={i === 0} />
          ))}
        </div>
      )}
      <LayoutMenu />
      {suggest && (
        <button onClick={() => applyLayout(suggest)} className="h-7 rounded-[4px] border border-accent px-2.5 text-[12px] font-medium text-accent hover:bg-desk-raised">
          Switch to {label(suggest)}
        </button>
      )}
    </div>
  );
}

// A saved layout: click to apply it, right-click (or the Menu key) to delete it.
function SavedLayoutButton({ id, label, on, first }: { id: string; label: string; on: boolean; first: boolean }) {
  const { applyLayout, deleteLayout } = useWindows.getState();
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  const [confirm, setConfirm] = useState(false);
  const btn = useRef<HTMLButtonElement | null>(null);
  const menu = useRef<HTMLDivElement | null>(null);

  const close = () => {
    setAt(null);
    setConfirm(false);
  };
  // Close on a click elsewhere, Escape, or scrolling away; focus the menu when it opens.
  useEffect(() => {
    if (!at) return;
    menu.current?.querySelector<HTMLElement>("button")?.focus();
    const down = (e: MouseEvent) => !menu.current?.contains(e.target as Node) && close();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
        btn.current?.focus();
      }
    };
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    window.addEventListener("blur", close);
    return () => {
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", key);
      window.removeEventListener("blur", close);
    };
  }, [at]);

  return (
    <>
      <button
        ref={btn}
        aria-pressed={on}
        aria-haspopup="menu"
        onClick={() => applyLayout(id)}
        onContextMenu={(e) => {
          e.preventDefault();
          // The Menu key fires contextmenu at 0,0: place the menu under the button instead.
          const r = e.currentTarget.getBoundingClientRect();
          setAt(e.clientX || e.clientY ? { x: e.clientX, y: e.clientY } : { x: r.left, y: r.bottom + 4 });
          setConfirm(false);
        }}
        className={`${seg(on, first)} max-w-32 truncate`}
      >
        {label}
      </button>
      {at && (
        <div ref={menu} role="menu" aria-label={`${label} layout`} style={{ left: Math.min(at.x, window.innerWidth - 232), top: at.y }} className="fixed z-[200000] w-56 rounded-[6px] border border-desk-line-strong bg-desk p-1 text-desk-ink">
          <button
            role="menuitem"
            onClick={() => {
              if (!confirm) return setConfirm(true);
              deleteLayout(id);
              close();
            }}
            className={`${menuItem} ${confirm ? "bg-desk-bad font-semibold text-desk hover:bg-desk-bad focus:bg-desk-bad" : "text-desk-bad"}`}
          >
            {confirm ? `Confirm delete ${label}` : `Delete ${label}`}
          </button>
        </div>
      )}
    </>
  );
}

const menuItem = "flex h-8 w-full items-center gap-2 rounded-[4px] px-2.5 text-left text-[13px] hover:bg-desk-raised focus:bg-desk-raised";

// The + button: everything about layouts that isn't picking one.
function LayoutMenu() {
  const layout = useWindows((s) => s.layout);
  const dirty = useWindows((s) => s.dirty);
  const { applyLayout, saveAs } = useWindows.getState();
  const { open, setOpen, root, trigger } = usePopover();
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const menu = useRef<HTMLDivElement | null>(null);

  // Each time it opens, start on the menu, not the name form.
  useEffect(() => {
    if (!open) setNaming(false);
    else menu.current?.querySelector<HTMLElement>("[role^=menuitem]")?.focus();
  }, [open]);

  const close = () => {
    setOpen(false);
    trigger.current?.focus();
  };

  // Up and down arrows move between items.
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const items = [...(menu.current?.querySelectorAll<HTMLElement>("[role^=menuitem]") ?? [])];
    const i = items.indexOf(document.activeElement as HTMLElement);
    e.preventDefault();
    items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
  };

  return (
    <div ref={root} className="relative">
      <button
        ref={trigger}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Layout options"
        onClick={() => setOpen((o) => !o)}
        className={`grid h-7 w-7 place-items-center rounded-[4px] border border-desk-line-strong text-[16px] leading-none text-desk-ink hover:bg-desk-raised ${open ? "bg-desk-raised" : ""}`}
      >
        +
      </button>
      {open && (
        <div ref={menu} role="menu" aria-label="Layout options" onKeyDown={onKey} className="absolute top-full left-0 z-50 mt-1 w-60 rounded-[6px] border border-desk-line-strong bg-desk p-1 text-desk-ink">
          {naming ? (
            <form
              className="flex items-center gap-1 p-1"
              onSubmit={(e) => {
                e.preventDefault();
                saveAs(name);
                setName("");
                close();
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
                placeholder="Layout name"
                className="h-8 min-w-0 flex-1 rounded-[4px] border border-desk-line-strong bg-desk-raised px-2 text-[13px] text-desk-ink placeholder:text-desk-muted focus:border-accent"
              />
              <button className="h-8 rounded-[4px] bg-accent px-2.5 text-[12px] font-semibold text-accent-ink">Save</button>
            </form>
          ) : (
            <>
              <button role="menuitem" onClick={() => setNaming(true)} className={menuItem}>
                Save layout as…
              </button>
              {dirty && (
                <button
                  role="menuitem"
                  onClick={() => {
                    applyLayout(layout, { reset: true });
                    close();
                  }}
                  className={menuItem}
                >
                  Reset layout
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
