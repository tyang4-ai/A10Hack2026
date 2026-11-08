"use client";

import { useEffect, useRef, useState } from "react";
import type { Run } from "@contract/types";
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

export const STATUS_DOT: Record<Run["status"], string> = {
  running: "bg-desk-link",
  awaiting_approval: "bg-accent",
  done: "bg-desk-good",
  stopped: "bg-desk-bad",
  failed: "bg-desk-bad",
};
export const STATUS_WORD: Record<Run["status"], string> = {
  running: "Running",
  awaiting_approval: "Waiting for approval",
  done: "Done",
  stopped: "Stopped",
  failed: "Failed",
};

export function ago(iso: string) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
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
          <div className="px-2.5 py-1.5 text-[11px] text-desk-muted">Theme</div>
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
