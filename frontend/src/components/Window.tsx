"use client";

// One window: a 32px title bar, a body, and resize handles on every edge and corner.
// While dragging, geometry lives in local state, so only this window re-renders;
// the store is updated once, when the pointer is released.
import { memo, useMemo, useRef, useState } from "react";
import { clampGeo, KEEP_PX, MODULE_KINDS, TITLE_PX, useWindows, type Geo, type Win } from "@/lib/windows";
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

// The title bar under a point, if the window on top there is showing its title bar at that spot.
function titleBarAt(self: string, px: number, py: number, W: number, H: number) {
  const over = Object.values(useWindows.getState().wins)
    .filter((w) => w.id !== self && !w.min && !w.max && px >= w.x * W && px < (w.x + w.w) * W && py >= w.y * H && py < (w.y + w.h) * H)
    .sort((a, b) => b.z - a.z)[0];
  return over && py < over.y * H + TITLE_PX ? over : null;
}

// The visible, unmaximized window (other than the one being dragged) under a point on the desk.
function tileAt(self: string, fx: number, fy: number) {
  return Object.values(useWindows.getState().wins).find((w) => w.id !== self && !w.min && !w.max && fx >= w.x && fx < w.x + w.w && fy >= w.y && fy < w.y + w.h) ?? null;
}

// Dropping onto empty desk fills the empty space there: the largest rectangle that
// contains the pointer and overlaps no other window. Candidate edges are the desk's
// edges and every other window's edges; for each left/right pair around the pointer,
// the nearest windows above and below bound the height.
export function emptyAt(self: string, fx: number, fy: number, minW: number, minH: number): Geo | null {
  const eps = 1e-6;
  const others = Object.values(useWindows.getState().wins).filter((w) => w.id !== self && !w.min && !w.max);
  if (others.some((w) => fx > w.x && fx < w.x + w.w && fy > w.y && fy < w.y + w.h)) return null;
  const xs = [...new Set([0, 1, ...others.flatMap((w) => [w.x, w.x + w.w])])].sort((a, b) => a - b);
  let best: Geo | null = null;
  for (const l of xs.filter((x) => x <= fx))
    for (const r of xs.filter((x) => x > fx)) {
      if (r - l < minW) continue;
      let top = 0;
      let bottom = 1;
      let blocked = false;
      for (const w of others) {
        if (w.x >= r - eps || w.x + w.w <= l + eps) continue; // not in this column band
        if (w.y + w.h <= fy + eps) top = Math.max(top, w.y + w.h);
        else if (w.y >= fy - eps) bottom = Math.min(bottom, w.y);
        else blocked = true; // a window crosses the pointer's row inside this band
      }
      if (blocked) continue;
      const g = { x: l, y: top, w: r - l, h: bottom - top };
      if (g.h >= minH && (!best || g.w * g.h > best.w * best.h)) best = g;
    }
  return best;
}

// Resizing moves an edge line, not just one window's edge. Every window with an
// edge on that line, touching the dragged window's span (or a window already in
// the group), moves with it, so two windows side by side resize together.
// A window that overlaps a group member (stacked in front or behind) never joins.
// Hold Alt to resize only the one window.
type Side = "n" | "s" | "e" | "w";
interface EdgeGroup {
  axis: "x" | "y";
  line: number;
  ends: string[]; // windows whose right (or bottom) edge is on the line
  starts: string[]; // windows whose left (or top) edge is on the line
}

function edgeGroup(id: string, side: Side, wins: Record<string, Win>, alone: boolean): EdgeGroup {
  const eps = 1e-4;
  const a = wins[id];
  const axis = side === "e" || side === "w" ? "x" : "y";
  const pos = (w: Win) => (axis === "x" ? w.x : w.y);
  const size = (w: Win) => (axis === "x" ? w.w : w.h);
  const cpos = (w: Win) => (axis === "x" ? w.y : w.x);
  const csize = (w: Win) => (axis === "x" ? w.h : w.w);
  const atEnd = side === "e" || side === "s";
  const line = atEnd ? pos(a) + size(a) : pos(a);
  const g: EdgeGroup = { axis, line, ends: atEnd ? [id] : [], starts: atEnd ? [] : [id] };
  if (alone) return g;
  let lo = cpos(a);
  let hi = cpos(a) + csize(a);
  const others = Object.values(wins).filter((w) => w.id !== id && !w.min && !w.max);
  // Only side-by-side neighbours: a window stacked over or under one already in the group
  // (overlapping it, not just touching) is left alone.
  const overlaps = (p: Win, q: Win) => p.x < q.x + q.w - eps && q.x < p.x + p.w - eps && p.y < q.y + q.h - eps && q.y < p.y + p.h - eps;
  const group = [a];
  for (let grew = true; grew; ) {
    grew = false;
    for (const w of others) {
      if (g.ends.includes(w.id) || g.starts.includes(w.id)) continue;
      if (group.some((m) => overlaps(m, w))) continue;
      if (cpos(w) >= hi - eps || cpos(w) + csize(w) <= lo + eps) continue;
      const end = Math.abs(pos(w) + size(w) - line) < eps;
      const start = Math.abs(pos(w) - line) < eps;
      if (!end && !start) continue;
      (end ? g.ends : g.starts).push(w.id);
      group.push(w);
      lo = Math.min(lo, cpos(w));
      hi = Math.max(hi, cpos(w) + csize(w));
      grew = true;
    }
  }
  return g;
}

// Move each group's line by the drag, clamped so no window goes under its minimum size.
function applyGroups(groups: EdgeGroup[], orig: Record<string, Win>, dx: number, dy: number, minW: number, minH: number): Record<string, Geo> {
  const out: Record<string, Geo> = {};
  const geo = (id: string): Geo => out[id] ?? { x: orig[id].x, y: orig[id].y, w: orig[id].w, h: orig[id].h };
  for (const g of groups) {
    const x = g.axis === "x";
    const min = x ? minW : minH;
    const P = (q: Geo) => (x ? q.x : q.y);
    const S = (q: Geo) => (x ? q.w : q.h);
    let lo = 0;
    let hi = 1;
    for (const id of g.ends) lo = Math.max(lo, P(geo(id)) + min);
    for (const id of g.starts) hi = Math.min(hi, P(geo(id)) + S(geo(id)) - min);
    const L = Math.min(Math.max(g.line + (x ? dx : dy), lo), Math.max(lo, hi));
    for (const id of g.ends) {
      const q = geo(id);
      out[id] = x ? { ...q, w: L - q.x } : { ...q, h: L - q.y };
    }
    for (const id of g.starts) {
      const q = geo(id);
      const end = P(q) + S(q);
      out[id] = x ? { ...q, x: L, w: end - L } : { ...q, y: L, h: end - L };
    }
  }
  return out;
}

// Filling empty space only triggers from a small square at the centre of that space,
// so a window can still be dropped freely anywhere else in it.
const TARGET_PX = 72;
function targetSquare(region: Geo, desk: DOMRect): Geo {
  const s = Math.min(TARGET_PX, region.w * desk.width * 0.5, region.h * desk.height * 0.5);
  const cx = (region.x + region.w / 2) * desk.width;
  const cy = (region.y + region.h / 2) * desk.height;
  return { x: (cx - s / 2) / desk.width, y: (cy - s / 2) / desk.height, w: s / desk.width, h: s / desk.height };
}
function emptyRegion(id: string, px: number, py: number, desk: DOMRect) {
  return emptyAt(id, px / desk.width, py / desk.height, Math.min(1, MIN_W / desk.width), Math.min(1, MIN_H / desk.height));
}
// The empty region to fill, if the pointer is inside its centre square.
function fillTarget(id: string, px: number, py: number, desk: DOMRect): Geo | null {
  const region = emptyRegion(id, px, py, desk);
  if (!region) return null;
  const t = targetSquare(region, desk);
  const fx = px / desk.width;
  const fy = py / desk.height;
  return fx >= t.x && fx <= t.x + t.w && fy >= t.y && fy <= t.y + t.h ? region : null;
}
// The centre square to show while the pointer is over empty space but not on it yet.
function hintAt(id: string, px: number, py: number, desk: DOMRect): Geo | null {
  const region = emptyRegion(id, px, py, desk);
  return region ? targetSquare(region, desk) : null;
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
  const { focus, minimize, toggleMax, close, resize, swap } = useWindows.getState();
  const [live, setLive] = useState<Geo | null>(null);
  const liveRef = useRef<Geo | null>(null);
  const peek = useWindows((s) => s.peek[id]);
  const drag = useRef<{
    mode: Mode;
    sx: number;
    sy: number;
    g: Geo;
    orig: Geo;
    desk: DOMRect;
    moved: boolean;
    target: string | null;
    groups: EdgeGroup[];
    wins: Record<string, Win>;
    result: Record<string, Geo> | null;
  } | null>(null);

  // The body only re-renders when its own data changes, never while the window moves.
  const kind = win?.kind;
  const ref = win?.ref;
  const body = useMemo(() => (kind ? <WindowBody kind={kind} wref={ref} /> : null), [kind, ref]);

  if (!win) return null;
  const meta = KIND_META[win.kind];
  const title = winTitle(win);
  const geo: Geo = live ?? peek ?? (win.max ? FULL : win);

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
    const wins = useWindows.getState().wins;
    const groups = mode === "move" ? [] : (mode.split("") as Side[]).map((side) => edgeGroup(id, side, wins, e.altKey));
    drag.current = { mode, sx: e.clientX, sy: e.clientY, g, orig, desk, moved: false, target: null, groups, wins, result: null };
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
      // Dragging into the bottom, left or right edge: the edge side stays put and the window
      // gets smaller, down to its minimum size. Past that it slides off, title bar still reachable.
      const minW = Math.min(d.g.w, MIN_W / d.desk.width);
      const minH = Math.min(d.g.h, MIN_H / d.desk.height);
      let x = d.g.x + dx;
      let w = d.g.w;
      let y = d.g.y + dy;
      let h = d.g.h;
      const pushRight = x + w > 1;
      const pushLeft = x < 0;
      const pushDown = y + h > 1;
      const pushing = pushRight || pushLeft || pushDown;
      if (pushRight) w = Math.max(minW, 1 - x);
      else if (pushLeft) {
        const right = x + d.g.w;
        w = Math.max(minW, right);
        x = right - w;
      }
      if (pushDown) h = Math.max(minH, 1 - y);
      setGeo(clampGeo({ x, y, w, h }, TITLE_PX / d.desk.height, KEEP_PX / d.desk.width));
      const px = e.clientX - d.desk.left;
      const py = e.clientY - d.desk.top;
      // The space a window was just lifted from looks empty, so the fill target waits
      // until the pointer has left the window's original area.
      const o = d.orig;
      const leftHome = !(px >= o.x * d.desk.width && px <= (o.x + o.w) * d.desk.width && py >= o.y * d.desk.height && py <= (o.y + o.h) * d.desk.height);
      let snap: Geo | null;
      if (useWindows.getState().auto) {
        // Auto tiling: preview the tile under the pointer; dropping there swaps the two windows.
        const over = tileAt(id, px / d.desk.width, py / d.desk.height);
        d.target = over?.id ?? null;
        snap = over ? { x: over.x, y: over.y, w: over.w, h: over.h } : null;
      } else {
        // Desk edges first, then another window's title bar (drop to swap), then the empty space under the pointer.
        // Filling needs a drop on the small centre square, so it stays on even while the window
        // is pushed into an edge; only the title bar swap waits until the window is clear of the edges.
        const edge = snapFor(px, py, d.desk.width, d.desk.height);
        const bar = edge || pushing ? null : titleBarAt(id, px, py, d.desk.width, d.desk.height);
        d.target = bar?.id ?? null;
        snap =
          edge ??
          (bar ? { x: bar.x, y: bar.y, w: bar.w, h: bar.h } : null) ??
          (leftHome ? fillTarget(id, px, py, d.desk) : null);
      }
      const cur = useWindows.getState();
      const hint = !snap && !cur.auto && leftHome ? hintAt(id, px, py, d.desk) : null;
      if (JSON.stringify(snap) !== JSON.stringify(cur.snap) || JSON.stringify(hint) !== JSON.stringify(cur.snapHint)) useWindows.setState({ snap, snapHint: hint });
    } else {
      const res = applyGroups(d.groups, d.wins, dx, dy, Math.min(1, MIN_W / d.desk.width), Math.min(1, MIN_H / d.desk.height));
      d.result = res;
      setGeo(res[id]);
      // Neighbours on the same edge line follow live, through the store's transient peek.
      const others = Object.fromEntries(Object.entries(res).filter(([k]) => k !== id));
      if (Object.keys(others).length) useWindows.setState({ peek: others });
    }
  };

  const onUp = () => {
    const d = drag.current;
    if (!d) return;
    drag.current = null;
    const snap = useWindows.getState().snap;
    useWindows.setState({ snap: null, snapHint: null });
    const g = liveRef.current;
    setGeo(null);
    if (d.result) {
      useWindows.getState().resizeMany(d.result);
      return;
    }
    if (!d.moved || !g) return;
    if (useWindows.getState().auto && d.mode === "move") {
      // Dropped on another tile: swap. Dropped anywhere else: snap back into place.
      if (d.target) swap(id, d.target);
      else useWindows.getState().tile();
      return;
    }
    if (d.target) return useWindows.getState().swapPlaces(id, d.target);
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
      className={`win absolute flex animate-[win-in_120ms_ease-out] flex-col border bg-desk ${focused ? "border-accent" : "border-desk-line-strong"} ${win.kind === "brief" ? "print-brief" : ""} ${
        // Glide to a new place (swap, snap, layout change), but follow the pointer exactly while dragging or resizing.
        live || peek ? "" : "transition-[left,top,width,height] duration-150 ease-out"
      }`}
    >
      <div
        data-titlebar
        tabIndex={0}
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
