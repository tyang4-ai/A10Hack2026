"use client";

// One window: a 32px title bar, a body, and resize handles on every edge and corner.
// While dragging, geometry lives in local state, so only this window re-renders;
// the store is updated once, when the pointer is released.
import { memo, useMemo, useRef, useState } from "react";
import { clampGeo, MODULE_KINDS, useWindows, type Geo } from "@/lib/windows";
import { KIND_META, TitleExtras, WindowBody, WinSubtitle, winTitle } from "./WindowContent";

type Mode = "move" | "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
const FULL: Geo = { x: 0, y: 0, w: 1, h: 1 };
const MIN_W = 320;
const MIN_H = 200;

// Dragging near an edge previews a snap: left or right half, a quarter near a corner, maximized at the top.
export function snapFor(px: number, py: number, W: number, H: number): Geo | null {
  const T = 12;
  const cx = Math.min(140, W / 4);
  const cy = Math.min(120, H / 4);
  const L = px <= T;
  const R = px >= W - T;
  const top = py <= T;
  const bottom = py >= H - T;
  if (!L && !R && !top && !bottom) return null;
  const left = L || ((top || bottom) && px < cx);
  const right = R || ((top || bottom) && px > W - cx);
  const up = top || ((L || R) && py < cy);
  const down = bottom || ((L || R) && py > H - cy);
  if ((left || right) && (up || down)) return { x: left ? 0 : 0.5, y: up ? 0 : 0.5, w: 0.5, h: 0.5 };
  if (left) return { x: 0, y: 0, w: 0.5, h: 1 };
  if (right) return { x: 0.5, y: 0, w: 0.5, h: 1 };
  if (top) return FULL;
  return null;
}

function resizeGeo(g: Geo, mode: Mode, dx: number, dy: number, minW: number, minH: number): Geo {
  let { x, y, w, h } = g;
  if (mode.includes("e")) w = Math.min(Math.max(g.w + dx, minW), 1 - g.x);
  if (mode.includes("s")) h = Math.min(Math.max(g.h + dy, minH), 1 - g.y);
  if (mode.includes("w")) {
    x = Math.min(Math.max(g.x + dx, 0), g.x + g.w - minW);
    w = g.w + g.x - x;
  }
  if (mode.includes("n")) {
    y = Math.min(Math.max(g.y + dy, 0), g.y + g.h - minH);
    h = g.h + g.y - y;
  }
  return { x, y, w, h };
}

const HANDLES: [Exclude<Mode, "move">, string][] = [
  ["n", "-top-[3px] left-3 right-3 h-[6px] cursor-ns-resize"],
  ["s", "-bottom-[3px] left-3 right-3 h-[6px] cursor-ns-resize"],
  ["e", "-right-[3px] top-3 bottom-3 w-[6px] cursor-ew-resize"],
  ["w", "-left-[3px] top-3 bottom-3 w-[6px] cursor-ew-resize"],
  ["nw", "-top-[3px] -left-[3px] h-3 w-3 cursor-nwse-resize"],
  ["se", "-bottom-[3px] -right-[3px] h-3 w-3 cursor-nwse-resize"],
  ["ne", "-top-[3px] -right-[3px] h-3 w-3 cursor-nesw-resize"],
  ["sw", "-bottom-[3px] -left-[3px] h-3 w-3 cursor-nesw-resize"],
];

const tbBtn = "grid h-6 w-6 flex-none place-items-center rounded-[4px] text-desk-muted hover:bg-desk-raised hover:text-desk-ink";

export const Window = memo(function Window({ id }: { id: string }) {
  const win = useWindows((s) => s.wins[id]);
  const focused = useWindows((s) => s.focused === id);
  const { focus, minimize, toggleMax, close, resize } = useWindows.getState();
  const [live, setLive] = useState<Geo | null>(null);
  const liveRef = useRef<Geo | null>(null);
  const drag = useRef<{ mode: Mode; sx: number; sy: number; g: Geo; orig: Geo; desk: DOMRect; moved: boolean } | null>(null);

  // The body only re-renders when its own data changes, never while the window moves.
  const kind = win?.kind;
  const ref = win?.ref;
  const body = useMemo(() => (kind ? <WindowBody kind={kind} wref={ref} /> : null), [kind, ref]);

  if (!win) return null;
  const meta = KIND_META[win.kind];
  const title = winTitle(win);
  const geo: Geo = live ?? (win.max ? FULL : win);

  const setGeo = (g: Geo | null) => {
    liveRef.current = g;
    setLive(g);
  };

  const start = (e: React.PointerEvent<HTMLElement>, mode: Mode) => {
    if (e.button !== 0) return;
    if (mode === "move" && (e.target as HTMLElement).closest("button")) return;
    const deskEl = e.currentTarget.closest("[data-desk]");
    if (!deskEl) return;
    const desk = deskEl.getBoundingClientRect();
    e.currentTarget.setPointerCapture(e.pointerId);
    e.preventDefault();
    const orig: Geo = { x: win.x, y: win.y, w: win.w, h: win.h };
    let g: Geo = win.max ? FULL : orig;
    // Dragging a maximized window restores its size under the pointer.
    if (win.max && mode === "move") g = clampGeo({ ...orig, x: (e.clientX - desk.left) / desk.width - orig.w / 2, y: 0 });
    drag.current = { mode, sx: e.clientX, sy: e.clientY, g, orig, desk, moved: false };
    focus(id);
  };

  const onMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    if (!d.moved && Math.abs(e.clientX - d.sx) + Math.abs(e.clientY - d.sy) < 3) return;
    d.moved = true;
    const dx = (e.clientX - d.sx) / d.desk.width;
    const dy = (e.clientY - d.sy) / d.desk.height;
    if (d.mode === "move") {
      setGeo(clampGeo({ ...d.g, x: d.g.x + dx, y: d.g.y + dy }));
      const snap = snapFor(e.clientX - d.desk.left, e.clientY - d.desk.top, d.desk.width, d.desk.height);
      const cur = useWindows.getState().snap;
      if (JSON.stringify(snap) !== JSON.stringify(cur)) useWindows.setState({ snap });
    } else {
      setGeo(resizeGeo(d.g, d.mode, dx, dy, Math.min(1, MIN_W / d.desk.width), Math.min(1, MIN_H / d.desk.height)));
    }
  };

  const onUp = () => {
    const d = drag.current;
    if (!d) return;
    drag.current = null;
    const snap = useWindows.getState().snap;
    useWindows.setState({ snap: null });
    const g = liveRef.current;
    setGeo(null);
    if (!d.moved || !g) return;
    if (snap && snap.w === 1 && snap.h === 1) resize(id, d.orig, true);
    else resize(id, snap ?? g);
  };

  // Keyboard: arrows move the focused title bar's window, Shift + arrows resize it.
  const onKey = (e: React.KeyboardEvent) => {
    const step = 0.02;
    const dir = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (!dir) return;
    e.preventDefault();
    const g = win.max ? FULL : win;
    if (e.shiftKey) resize(id, { ...g, w: g.w + dir[0], h: g.h + dir[1] });
    else resize(id, { ...g, x: g.x + dir[0], y: g.y + dir[1] });
  };

  return (
    <section
      data-win={id}
      aria-label={`${title} window`}
      hidden={win.min}
      onPointerDownCapture={() => focus(id)}
      onFocusCapture={() => focus(id)}
      style={{ left: `${geo.x * 100}%`, top: `${geo.y * 100}%`, width: `${geo.w * 100}%`, height: `${geo.h * 100}%`, zIndex: win.z }}
      className={`win absolute flex animate-[win-in_120ms_ease-out] flex-col border bg-desk ${focused ? "border-accent" : "border-desk-line-strong"} ${win.kind === "brief" ? "print-brief" : ""}`}
    >
      <div
        data-titlebar
        tabIndex={0}
        title="Drag to move. Double-click to maximize. Arrow keys move, Shift with arrows resizes."
        onPointerDown={(e) => start(e, "move")}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onDoubleClick={(e) => !(e.target as HTMLElement).closest("button") && toggleMax(id)}
        onKeyDown={onKey}
        className="win-titlebar flex h-8 flex-none cursor-default touch-none items-center gap-2 border-b border-desk-line bg-desk pr-1 pl-2.5 text-[12px] text-desk-ink select-none focus-visible:outline-offset-[-2px]"
      >
        <span aria-hidden className={`grid h-4 min-w-4 place-items-center font-mono text-[11px] ${focused ? "text-accent" : "text-desk-muted"}`}>
          {meta.icon}
        </span>
        <span className={`truncate font-semibold ${focused ? "text-white" : ""} ${win.kind === "agent" && win.ref ? "font-mono" : ""}`}>{title}</span>
        <span className="min-w-0 flex-1 truncate text-desk-muted">
          <WinSubtitle kind={win.kind} wref={win.ref} />
        </span>
        <TitleExtras kind={win.kind} wref={win.ref} />
        <button className={tbBtn} aria-label={`Minimize ${title}`} title="Minimize to the dock" onClick={() => minimize(id)}>
          –
        </button>
        <button className={tbBtn} aria-label={win.max ? `Restore ${title}` : `Maximize ${title}`} title={win.max ? "Restore" : "Maximize"} onClick={() => toggleMax(id)}>
          {win.max ? "❐" : "□"}
        </button>
        {!MODULE_KINDS.includes(win.kind) && (
          <button className={tbBtn} aria-label={`Close ${title}`} title="Close" onClick={() => close(id)}>
            ✕
          </button>
        )}
      </div>
      <div className={`win-body min-h-0 flex-1 overflow-auto ${meta.paper ? "bg-paper text-ink" : "bg-desk text-desk-ink"}`}>{body}</div>
      {!win.max &&
        HANDLES.map(([mode, cls]) => (
          <div key={mode} aria-hidden onPointerDown={(e) => start(e, mode)} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp} className={`win-handle absolute touch-none ${cls}`} />
        ))}
    </section>
  );
});
