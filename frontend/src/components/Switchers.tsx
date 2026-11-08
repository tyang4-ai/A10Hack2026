"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { Run } from "@contract/types";
import { STAGE_LABEL } from "@/lib/format";
import { useRunStore } from "@/lib/store";
import { applyTheme, readTheme, THEMES, type ThemeId } from "@/lib/theme";

// Close a popover on outside click or Escape, and return focus to its trigger.
function usePopover() {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => !root.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return { open, setOpen, root, trigger };
}

const STATUS_DOT: Record<Run["status"], string> = {
  running: "bg-desk-link",
  awaiting_approval: "bg-accent",
  done: "bg-desk-good",
  stopped: "bg-desk-bad",
  failed: "bg-desk-bad",
};
const STATUS_WORD: Record<Run["status"], string> = {
  running: "Running",
  awaiting_approval: "Waiting for approval",
  done: "Done",
  stopped: "Stopped",
  failed: "Failed",
};

function ago(iso: string) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

// Replaces the native <select>: shows what each run is, not just its id.
export function RunSwitcher() {
  const runs = useRunStore((s) => s.runs);
  const runId = useRunStore((s) => s.runId);
  const current = useRunStore((s) => s.run);
  const openRun = useRunStore((s) => s.openRun);
  const loadRuns = useRunStore((s) => s.loadRuns);
  const { open, setOpen, root, trigger } = usePopover();
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const list = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return needle ? runs.filter((r) => `${r.company} ${r.run_id} ${r.ticker ?? ""}`.toLowerCase().includes(needle)) : runs;
  }, [runs, q]);

  useEffect(() => {
    if (open) {
      void loadRuns();
      setQ("");
      setActive(Math.max(0, runs.findIndex((r) => r.run_id === runId)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const choose = (r: Run) => {
    setOpen(false);
    if (r.run_id !== runId) void openRun(r.run_id);
    trigger.current?.focus();
  };

  return (
    <div ref={root} className="relative">
      <button
        ref={trigger}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className={`flex h-8 max-w-80 items-center gap-2 rounded-[4px] px-2.5 text-left transition-colors hover:bg-desk-raised ${open ? "bg-desk-raised" : ""}`}
      >
        {current ? (
          <>
            <span className={`h-2 w-2 flex-none rounded-full ${STATUS_DOT[current.status]}`} />
            <span className="truncate font-semibold text-white">{current.company}</span>
            <span className="flex-none font-mono text-[11px] text-desk-muted" title={current.run_id}>{current.run_id.split("_").pop()}</span>
          </>
        ) : (
          <span className="text-desk-muted">Choose a run</span>
        )}
        <svg aria-hidden width="10" height="10" viewBox="0 0 10 10" className="ml-0.5 flex-none text-desk-muted">
          <path d="M2 4l3 3 3-3" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </button>

      {open && (
        <div className="absolute top-full left-0 z-50 mt-1 w-[420px] overflow-hidden rounded-[6px] border border-desk-line-strong bg-desk text-desk-ink">
          <div className="border-b border-desk-line p-2">
            <label className="sr-only" htmlFor="run-search">
              Find a run
            </label>
            <input
              id="run-search"
              autoFocus
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setActive(0);
              }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setActive((a) => Math.min(list.length - 1, a + 1));
                } else if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setActive((a) => Math.max(0, a - 1));
                } else if (e.key === "Enter" && list[active]) {
                  e.preventDefault();
                  choose(list[active]);
                }
              }}
              placeholder="Find by company or run id"
              aria-controls="run-list"
              aria-activedescendant={list[active] ? `run-opt-${list[active].run_id}` : undefined}
              className="h-8 w-full rounded-[4px] border border-desk-line bg-desk-raised px-2.5 text-[13px] outline-none placeholder:text-desk-muted focus:border-desk-muted"
            />
          </div>
          <ul id="run-list" role="listbox" aria-label="Runs" className="max-h-80 overflow-y-auto py-1">
            {list.map((r, i) => {
              const sel = r.run_id === runId;
              return (
                <li
                  key={r.run_id}
                  id={`run-opt-${r.run_id}`}
                  role="option"
                  aria-selected={sel}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => choose(r)}
                  className={`mx-1 flex cursor-pointer items-start gap-2.5 rounded-[4px] px-2.5 py-2 ${i === active ? "bg-desk-raised" : ""}`}
                >
                  <span className={`mt-1.5 h-2 w-2 flex-none rounded-full ${STATUS_DOT[r.status]}`} />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-baseline gap-2">
                      <span className="truncate font-semibold text-white">{r.company}</span>
                      {r.ticker && <span className="font-mono text-[11px] text-desk-muted">{r.ticker}</span>}
                      {r.mode !== "live" && <span className="rounded-[3px] bg-desk-warn px-1 font-mono text-[10px] font-semibold text-desk">{r.mode.toUpperCase()}</span>}
                    </span>
                    <span className="mt-0.5 flex gap-2 text-[12px] text-desk-muted">
                      <span>{STATUS_WORD[r.status]}</span>
                      {r.status !== "done" && <span>at {STAGE_LABEL[r.stage].toLowerCase()}</span>}
                      <span className="ml-auto font-mono text-[11px]">{r.run_id}</span>
                    </span>
                  </span>
                  <span className="mt-0.5 flex-none text-[11px] text-desk-muted">{ago(r.created_at)}</span>
                  {sel && (
                    <svg aria-hidden width="12" height="12" viewBox="0 0 12 12" className="mt-1 flex-none text-accent">
                      <path d="M2 6.5l2.5 2.5L10 3.5" fill="none" stroke="currentColor" strokeWidth="1.75" />
                    </svg>
                  )}
                </li>
              );
            })}
            {list.length === 0 && <li className="px-3 py-3 text-desk-muted">No runs match “{q}”.</li>}
          </ul>
        </div>
      )}
    </div>
  );
}

export function ThemeSwitcher() {
  const [theme, setTheme] = useState<ThemeId | null>(null);
  const { open, setOpen, root, trigger } = usePopover();
  useEffect(() => setTheme(readTheme()), []);
  const cur = THEMES.find((t) => t.id === theme) ?? THEMES[0];
  const pick = (t: ThemeId) => {
    applyTheme(t);
    setTheme(t);
  };
  return (
    <div ref={root} className="relative">
      <button
        ref={trigger}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Colour theme: ${cur.label}`}
        title="Colour theme"
        onClick={() => setOpen((o) => !o)}
        className={`flex h-8 items-center gap-1.5 rounded-[4px] px-2 hover:bg-desk-raised ${open ? "bg-desk-raised" : ""}`}
      >
        <Swatches s={cur.swatches} />
      </button>
      {open && (
        <div role="menu" aria-label="Colour themes" className="absolute top-full right-0 z-50 mt-1 w-64 rounded-[6px] border border-desk-line-strong bg-desk p-1 text-desk-ink">
          <div className="px-2.5 py-1.5 text-[11px] text-desk-muted">Colour theme. Hover to preview.</div>
          {THEMES.map((t) => (
            <button
              key={t.id}
              role="menuitemradio"
              aria-checked={t.id === cur.id}
              onMouseEnter={() => (document.documentElement.dataset.theme = t.id)}
              onMouseLeave={() => (document.documentElement.dataset.theme = cur.id)}
              onFocus={() => (document.documentElement.dataset.theme = t.id)}
              onClick={() => {
                pick(t.id);
                setOpen(false);
              }}
              className="flex w-full items-center gap-2.5 rounded-[4px] px-2.5 py-2 text-left hover:bg-desk-raised focus:bg-desk-raised"
            >
              <Swatches s={t.swatches} />
              <span className="flex-1">{t.label}</span>
              {t.id === cur.id && (
                <svg aria-hidden width="12" height="12" viewBox="0 0 12 12" className="text-accent">
                  <path d="M2 6.5l2.5 2.5L10 3.5" fill="none" stroke="currentColor" strokeWidth="1.75" />
                </svg>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Swatches({ s }: { s: readonly string[] }) {
  return (
    <span aria-hidden className="flex overflow-hidden rounded-full outline outline-1 outline-desk-line-strong">
      {s.map((c) => (
        <span key={c} className="h-3.5 w-3.5" style={{ background: c }} />
      ))}
    </span>
  );
}
