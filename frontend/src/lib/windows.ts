"use client";

// The window system: every panel is a window on one desk. Geometry is stored as
// fractions of the desk (0 to 1), so layouts scale when the browser is resized.
// Built-in layout presets follow the pipeline: each group of stages has one.
// The analyst can also save the current arrangement as a named profile. Each
// layout's geometry is saved to localStorage, so a reload restores it.
import type { Stage } from "@contract/types";
import { create } from "zustand";

export type WinKind = "sources" | "report" | "brief" | "inspector" | "traffic" | "agents" | "vault" | "log" | "policy" | "chain" | "agent" | "page" | "item";
export type BuiltIn = "approve" | "collect" | "watch" | "review" | "read" | "audit";
// A built-in preset, or "custom-<n>" for a profile the analyst saved.
export type LayoutName = string;

export interface Geo {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Win extends Geo {
  id: string;
  kind: WinKind;
  // agent: agent_id. page: "doc_id|page|item_id". item: "item:D-028", "source:S-001", "sentence:H-001" or "hole:HOLE-01".
  // An agent or page window without a ref follows the run (busiest agent, selected item's page).
  ref?: string;
  z: number;
  min: boolean;
  max: boolean;
  // Position in the auto tiling order: 0 is the main window on the left.
  slot?: number;
}

// The three module windows can be minimized but never closed.
export const MODULE_KINDS: WinKind[] = ["sources", "report", "brief"];
export const LAYOUTS: { id: BuiltIn; label: string }[] = [
  { id: "approve", label: "Approve" },
  { id: "collect", label: "Collect" },
  { id: "watch", label: "Watch" },
  { id: "review", label: "Review" },
  { id: "read", label: "Read" },
  { id: "audit", label: "Audit" },
];

// Each pipeline step maps to the preset that shows what that step is doing.
// Steps that show the same windows share a preset, so the desk doesn't jump on every step.
export const LAYOUT_FOR_STAGE: Record<Stage, BuiltIn> = {
  plan: "approve",
  approve: "approve",
  collect: "collect",
  extract: "watch",
  draft: "watch",
  verify: "review",
  repair: "review",
  summarize: "read",
  verify_summary: "read",
  complete: "read",
};

type Slot = Geo & { kind: WinKind; ref?: string };
const PRESETS: Record<BuiltIn, Slot[]> = {
  approve: [
    { kind: "sources", x: 0, y: 0, w: 0.6, h: 1 },
    { kind: "policy", x: 0.6, y: 0, w: 0.4, h: 0.5 },
    { kind: "inspector", x: 0.6, y: 0.5, w: 0.4, h: 0.5 },
  ],
  watch: [
    { kind: "traffic", x: 0, y: 0, w: 0.58, h: 0.46 },
    { kind: "agents", x: 0, y: 0.46, w: 0.29, h: 0.54 },
    { kind: "agent", x: 0.29, y: 0.46, w: 0.29, h: 0.54 },
    { kind: "inspector", x: 0.58, y: 0, w: 0.42, h: 0.46 },
    { kind: "page", x: 0.58, y: 0.46, w: 0.42, h: 0.54 },
  ],
  read: [
    { kind: "brief", x: 0, y: 0, w: 0.49, h: 1 },
    { kind: "chain", x: 0.49, y: 0, w: 0.51, h: 1 / 3 },
    { kind: "page", x: 0.49, y: 1 / 3, w: 0.51, h: 2 / 3 },
  ],
  collect: [
    { kind: "sources", x: 0, y: 0, w: 0.5, h: 1 },
    { kind: "traffic", x: 0.5, y: 0, w: 0.5, h: 0.55 },
    { kind: "policy", x: 0.5, y: 0.55, w: 0.5, h: 0.45 },
  ],
  review: [
    { kind: "report", x: 0, y: 0, w: 0.55, h: 1 },
    { kind: "inspector", x: 0.55, y: 0, w: 0.45, h: 0.5 },
    { kind: "page", x: 0.55, y: 0.5, w: 0.45, h: 0.5 },
  ],
  audit: [
    { kind: "policy", x: 0, y: 0, w: 0.4, h: 0.5 },
    { kind: "log", x: 0, y: 0.5, w: 0.4, h: 0.5 },
    { kind: "traffic", x: 0.4, y: 0, w: 0.6, h: 0.6 },
    { kind: "vault", x: 0.4, y: 0.6, w: 0.6, h: 0.4 },
  ],
};

export interface Custom {
  id: string;
  label: string;
  // The arrangement as it was saved; Reset goes back to this.
  wins: Win[];
}

export function winId(kind: WinKind, ref?: string): string {
  if (!ref) return kind;
  // One source page window per document page; the item only picks the highlight.
  if (kind === "page") {
    const [doc, page] = ref.split("|");
    return `page:${doc}:${page}`;
  }
  return `${kind}:${ref}`;
}

// Auto tiling: the first window takes the left 55%, the rest stack on the right
// (in two columns once there are more than three). Windows never overlap.
const MAIN_W = 0.55;
export function tileGeos(n: number): Geo[] {
  if (n <= 0) return [];
  if (n === 1) return [{ x: 0, y: 0, w: 1, h: 1 }];
  const rest = n - 1;
  const cols = rest > 3 ? 2 : 1;
  const rows = Math.ceil(rest / cols);
  const w = (1 - MAIN_W) / cols;
  const out: Geo[] = [{ x: 0, y: 0, w: MAIN_W, h: 1 }];
  for (let i = 0; i < rest; i++) {
    const col = Math.floor(i / rows);
    const inCol = Math.min(rows, rest - col * rows);
    const row = i % rows;
    out.push({ x: MAIN_W + col * w, y: row / inCol, w, h: 1 / inCol });
  }
  return out;
}

const clamp = (n: number, lo: number, hi: number) => Math.min(Math.max(n, lo), Math.max(lo, hi));

// Keep a window fully on the desk, so its title bar can never leave the screen.
export function clampGeo(g: Geo): Geo {
  const w = clamp(g.w, 0.05, 1);
  const h = clamp(g.h, 0.05, 1);
  return { w, h, x: clamp(g.x, 0, 1 - w), y: clamp(g.y, 0, 1 - h) };
}

// ---- persistence (best effort: the app works when storage is blocked) ----

const KEY = "axelrod.windows.v1";
interface Saved {
  layout: LayoutName;
  dirty: boolean;
  auto?: boolean;
  layouts: Record<string, Win[] | undefined>;
  customs?: Custom[];
}

function readSaved(): Saved | null {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Saved) : null;
  } catch {
    return null;
  }
}

function writeSaved(s: Saved) {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    // Storage blocked or full: the layout just won't survive a reload.
  }
}

// ---- store ----

interface WinState {
  wins: Record<string, Win>;
  focused: string | null;
  top: number;
  layout: LayoutName;
  // True once the user moves or resizes anything after a layout was applied.
  // Then a stage change suggests the next layout instead of applying it.
  dirty: boolean;
  suggest: LayoutName | null;
  snap: Geo | null;
  // Live geometry of neighbours while an edge they share is being dragged.
  peek: Record<string, Geo>;
  desk: { w: number; h: number };
  palette: boolean;
  hydrated: boolean;
  // Auto tiling: re-arrange visible windows whenever one opens, closes, minimizes or restores.
  auto: boolean;
  customs: Custom[];

  hydrate: () => void;
  open: (kind: WinKind, ref?: string, opts?: { beside?: boolean }) => void;
  close: (id: string) => void;
  focus: (id: string) => void;
  show: (id: string) => void;
  move: (id: string, x: number, y: number) => void;
  resize: (id: string, g: Geo, max?: boolean) => void;
  resizeMany: (geos: Record<string, Geo>) => void;
  minimize: (id: string) => void;
  toggleMax: (id: string) => void;
  applyLayout: (name: LayoutName, opts?: { reset?: boolean }) => void;
  setSuggest: (name: LayoutName | null) => void;
  closePinned: () => void;
  toggleAuto: () => void;
  tile: () => void;
  swap: (a: string, b: string) => void;
  saveAs: (label: string) => void;
  deleteLayout: (id: string) => void;
}

function presetWins(name: LayoutName, customs: Custom[]): Win[] {
  const custom = customs.find((c) => c.id === name);
  if (custom) return custom.wins;
  const slots = PRESETS[name as BuiltIn] ?? PRESETS.read;
  return slots.map((s) => ({ ...s, id: winId(s.kind, s.ref), z: 0, min: false, max: false, slot: undefined }));
}

function build(name: LayoutName, current: Record<string, Win>, saved: Win[] | null, customs: Custom[]): { wins: Record<string, Win>; top: number; focused: string | null } {
  const slots: Win[] = saved ?? presetWins(name, customs);
  const wins: Record<string, Win> = {};
  let top = 0;
  // Windows outside the layout stay open, minimized to the dock.
  for (const w of Object.values(current)) wins[w.id] = { ...w, min: true, max: false, z: ++top, slot: undefined };
  for (const kind of MODULE_KINDS) if (!wins[kind]) wins[kind] = { id: kind, kind, x: 0.1, y: 0.06, w: 0.6, h: 0.85, z: ++top, min: true, max: false };
  for (const s of [...slots].sort((a, b) => a.z - b.z)) wins[s.id] = { ...s, ...clampGeo(s), z: ++top };
  const visible = slots.filter((s) => !s.min).sort((a, b) => b.z - a.z);
  return { wins, top, focused: visible[0]?.id ?? null };
}

export const useWindows = create<WinState>((set, get) => ({
  wins: {},
  focused: null,
  top: 0,
  layout: "read",
  dirty: false,
  suggest: null,
  snap: null,
  peek: {},
  desk: { w: 1280, h: 720 },
  palette: false,
  hydrated: false,
  auto: false,
  customs: [],

  // Runs once on the client, after the first render, so server and client HTML match.
  hydrate: () => {
    if (get().hydrated) return;
    const saved = readSaved();
    const customs = saved?.customs ?? [];
    const layout = saved?.layout && (customs.some((c) => c.id === saved.layout) || saved.layout in PRESETS) ? saved.layout : "read";
    set({ ...build(layout, {}, saved?.layouts[layout] ?? null, customs), customs, layout, dirty: saved?.dirty ?? false, auto: saved?.auto ?? false, hydrated: true });
    if (get().auto) get().tile();
  },

  open: (kind, ref, opts) => {
    const id = winId(kind, ref);
    const { wins, top, focused } = get();
    const cur = wins[id];
    if (cur) {
      set({ wins: { ...wins, [id]: { ...cur, ref: ref ?? cur.ref, min: false, z: top + 1 } }, top: top + 1, focused: id });
      return;
    }
    const f = focused ? wins[focused] : null;
    let g: Geo;
    if (opts?.beside && f && !f.min) {
      // Beside the focused window: to its right if there is room, else to its left.
      const w = Math.min(f.w, 0.5);
      g = f.x + f.w + w <= 1 ? { x: f.x + f.w, y: f.y, w, h: f.h } : { x: Math.max(0, f.x - w), y: f.y, w, h: f.h };
    } else {
      // Cascade new windows so they don't stack exactly on top of each other.
      const n = Object.values(wins).filter((w) => !w.min).length % 8;
      g = { x: 0.16 + n * 0.025, y: 0.08 + n * 0.035, w: 0.42, h: 0.6 };
    }
    set({ wins: { ...wins, [id]: { id, kind, ref, ...clampGeo(g), z: top + 1, min: false, max: false } }, top: top + 1, focused: id });
  },

  close: (id) => {
    const { wins, focused } = get();
    if (!wins[id] || MODULE_KINDS.includes(wins[id].kind)) return;
    const next = { ...wins };
    delete next[id];
    set({ wins: next, focused: focused === id ? topVisible(next) : focused });
  },

  focus: (id) => {
    const { wins, top, focused } = get();
    const w = wins[id];
    if (!w || (focused === id && w.z === top)) return;
    set({ wins: { ...wins, [id]: { ...w, z: top + 1 } }, top: top + 1, focused: id });
  },

  // From the dock: restore a minimized window, then focus it.
  show: (id) => {
    const { wins, top } = get();
    const w = wins[id];
    if (!w) return;
    set({ wins: { ...wins, [id]: { ...w, min: false, z: top + 1 } }, top: top + 1, focused: id });
  },

  move: (id, x, y) => {
    const w = get().wins[id];
    if (w) get().resize(id, { x, y, w: w.w, h: w.h });
  },

  resize: (id, g, max = false) => {
    const { wins } = get();
    const w = wins[id];
    if (!w) return;
    // Placing a window by hand turns auto tiling off.
    set({ wins: { ...wins, [id]: { ...w, ...clampGeo(g), max } }, dirty: true, auto: false });
  },

  // Commit a shared-edge resize: every window on the dragged line at once.
  resizeMany: (geos) => {
    const wins = { ...get().wins };
    for (const [id, g] of Object.entries(geos)) if (wins[id]) wins[id] = { ...wins[id], ...clampGeo(g), max: false };
    set({ wins, peek: {}, dirty: true, auto: false });
  },

  minimize: (id) => {
    const { wins, focused } = get();
    const w = wins[id];
    if (!w) return;
    const next = { ...wins, [id]: { ...w, min: true } };
    set({ wins: next, focused: focused === id ? topVisible(next) : focused });
  },

  toggleMax: (id) => {
    const { wins, top } = get();
    const w = wins[id];
    if (w) set({ wins: { ...wins, [id]: { ...w, max: !w.max, min: false, z: top + 1 } }, top: top + 1, focused: id, dirty: true });
  },

  applyLayout: (name, opts) => {
    const cur = get().wins;
    // Saved windows pinned to an agent, page or item may belong to an older run: only keep the ones open now.
    const saved = opts?.reset ? null : (readSaved()?.layouts[name]?.filter((w) => !w.ref || !!cur[w.id]) ?? null);
    set({ ...build(name, cur, saved, get().customs), layout: name, dirty: false, suggest: null });
    if (get().auto) get().tile();
  },

  setSuggest: (suggest) => set({ suggest }),

  toggleAuto: () => {
    set({ auto: !get().auto });
    if (get().auto) get().tile();
  },

  // Keep the current order (slot, else left to right, top to bottom), then lay the windows out.
  tile: () => {
    const { wins } = get();
    const vis = Object.values(wins)
      .filter((w) => !w.min)
      .sort((a, b) => (a.slot ?? 1e9) - (b.slot ?? 1e9) || a.x - b.x || a.y - b.y);
    const geos = tileGeos(vis.length);
    const next = { ...wins };
    vis.forEach((w, i) => (next[w.id] = { ...w, ...geos[i], slot: i }));
    set({ wins: next });
  },

  // Save the current arrangement as a named profile and switch to it.
  saveAs: (label) => {
    const { customs, wins } = get();
    const n = customs.reduce((m, c) => Math.max(m, Number(c.id.slice(7)) || 0), 0) + 1;
    const custom: Custom = { id: `custom-${n}`, label: label.trim() || `Layout ${n}`, wins: Object.values(wins) };
    set({ customs: [...customs, custom], layout: custom.id, dirty: false, suggest: null });
  },

  deleteLayout: (id) => {
    const { customs, layout } = get();
    set({ customs: customs.filter((c) => c.id !== id) });
    const saved = readSaved();
    if (saved) {
      delete saved.layouts[id];
      writeSaved({ ...saved, customs: get().customs });
    }
    if (layout === id) get().applyLayout("read");
  },

  // Drag a window onto another in auto mode to swap their places.
  swap: (a, b) => {
    const { wins } = get();
    if (!wins[a] || !wins[b]) return;
    set({ wins: { ...wins, [a]: { ...wins[a], slot: wins[b].slot }, [b]: { ...wins[b], slot: wins[a].slot } }, dirty: true });
    get().tile();
  },

  // When the analyst opens a different run, windows pinned to the old run's agents, pages and items go away.
  closePinned: () => {
    const { wins, focused } = get();
    const next = Object.fromEntries(Object.entries(wins).filter(([, w]) => !(w.ref && (w.kind === "agent" || w.kind === "page" || w.kind === "item"))));
    set({ wins: next, focused: focused && next[focused] ? focused : topVisible(next) });
  },
}));

function topVisible(wins: Record<string, Win>): string | null {
  return Object.values(wins)
    .filter((w) => !w.min)
    .sort((a, b) => b.z - a.z)[0]?.id ?? null;
}

// Save the current layout's geometry whenever windows change.
useWindows.subscribe((s, prev) => {
  if (!s.hydrated || (s.wins === prev.wins && s.layout === prev.layout && s.dirty === prev.dirty && s.auto === prev.auto && s.customs === prev.customs)) return;
  const saved = readSaved();
  writeSaved({ layout: s.layout, dirty: s.dirty, auto: s.auto, customs: s.customs, layouts: { ...saved?.layouts, [s.layout]: Object.values(s.wins) } });
});

// In auto mode, re-tile whenever the set of visible windows changes.
const visibleKey = (wins: Record<string, Win>) =>
  Object.values(wins)
    .filter((w) => !w.min)
    .map((w) => w.id)
    .sort()
    .join("\n");
useWindows.subscribe((s, prev) => {
  if (s.auto && s.hydrated && visibleKey(s.wins) !== visibleKey(prev.wins)) s.tile();
});
