import type { CertaintyBand, Stage, Value, VerificationStatus } from "@contract/types";

// Round half up. toFixed rounds 5.85 to 5.8 because of float error, so shift
// by a tiny epsilon first (docs/CONTRACT.md section 11).
export function roundHalfUp(n: number, digits = 1): number {
  const f = 10 ** digits;
  return Math.sign(n) * Math.round(Math.abs(n) * f + 1e-9) / f;
}

const SCALE_WORD = { one: "", thousand: "thousand", million: "million", billion: "billion" } as const;
const CURRENCY: Record<string, string> = { USD: "$", EUR: "€", GBP: "£", JPY: "¥" };

// Format a reported value from its own unit and scale. Never assume a currency.
export function formatValue(v: Value | null): string {
  if (!v) return "—";
  const n = roundHalfUp(v.number, 1).toLocaleString("en-US", { maximumFractionDigits: 1 });
  if (v.unit === "percent" || v.unit === "%") return `${n}%`;
  const scale = SCALE_WORD[v.scale] ? ` ${SCALE_WORD[v.scale]}` : "";
  const cur = CURRENCY[v.unit];
  return cur ? `${cur}${n}${scale}` : `${n}${scale} ${v.unit}`.trim();
}

export function formatAxis(n: number, unit: string, scale: Value["scale"]): string {
  const cur = CURRENCY[unit] ?? "";
  const s = scale === "million" ? "m" : scale === "billion" ? "bn" : scale === "thousand" ? "k" : "";
  return `${cur}${roundHalfUp(n, 1).toLocaleString("en-US")}${s}`;
}

export const usd = (n: number) => `$${n < 1 ? n.toFixed(4) : n.toFixed(2)}`;
export const count = (n: number) => (n >= 10000 ? `${roundHalfUp(n / 1000, 1)}k` : n.toLocaleString("en-US"));
export const fmtTime = (iso: string) => new Date(iso).toLocaleTimeString("en-GB", { hour12: false });

export const STAGES: Stage[] = ["plan", "approve", "collect", "extract", "draft", "verify", "repair", "summarize", "verify_summary", "complete"];
export const STAGE_LABEL: Record<Stage, string> = {
  plan: "Plan",
  approve: "Approve",
  collect: "Collect",
  extract: "Extract",
  draft: "Draft",
  verify: "Verify",
  repair: "Repair",
  summarize: "Summarize",
  verify_summary: "Final check",
  complete: "Complete",
};

export type Module = "sources" | "report" | "brief";
export const moduleFor = (s: Stage): Module =>
  s === "plan" || s === "approve" || s === "collect" ? "sources" : s === "summarize" || s === "verify_summary" || s === "complete" ? "brief" : "report";

// Certainty bands. Light backgrounds for the brief, text colours elsewhere.
export const BAND_BG: Record<CertaintyBand, string> = {
  high: "bg-band-high",
  medium: "bg-band-medium",
  low: "bg-band-low",
  none: "bg-band-none",
};
export const BAND_TEXT: Record<CertaintyBand, string> = {
  high: "text-good",
  medium: "text-warn",
  low: "text-bad",
  none: "text-muted",
};

export const STATUS_TEXT: Record<VerificationStatus, string> = {
  pending: "text-muted",
  pass: "text-good",
  fixed: "text-link",
  fail: "text-bad",
  blocked: "text-warn",
};

export const CERTAINTY_HELP = "Support from checked evidence. Not the chance it's true.";

// Runs that are not live are labelled as simulated, in plain words (a hackathon rule too).
export const MODE_LABEL = "SIMULATED";
export const MODE_HELP: Record<"live" | "replay" | "fixture", string> = {
  live: "",
  fixture: "Recorded run. No live agents or AWS calls.",
  replay: "Recorded run, replayed at live speed. No live agents or AWS calls.",
};

export const human = (s: string) => s.replace(/_/g, " ");
