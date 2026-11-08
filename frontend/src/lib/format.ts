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
  // Two decimals below 10, so per-share amounts keep their cents (€0.95, not €1).
  const digits = Math.abs(v.number) < 10 ? 2 : 1;
  const n = roundHalfUp(v.number, digits).toLocaleString("en-US", { maximumFractionDigits: digits });
  if (v.unit === "percent" || v.unit === "%") return `${n}%`;
  const scale = SCALE_WORD[v.scale] ? ` ${SCALE_WORD[v.scale]}` : "";
  const cur = CURRENCY[v.unit];
  // Sign before the currency symbol: -€818 million, not €-818 million.
  const abs = roundHalfUp(Math.abs(v.number), digits).toLocaleString("en-US", { maximumFractionDigits: digits });
  return cur ? `${v.number < 0 ? "-" : ""}${cur}${abs}${scale}` : `${n}${scale} ${v.unit}`.trim();
}

// Compact chart labels from the value's own unit and scale: 13,429,000,000 EUR at scale
// "one" is €13.4bn, -1,918 at scale "million" is -€1.9bn, 5.9 with unit % is 5.9%.
const SCALE_MULT = { one: 1, thousand: 1e3, million: 1e6, billion: 1e9 } as const;
export function formatAxis(n: number, unit: string, scale: Value["scale"]): string {
  // One decimal below 100 so neighbours line up (€13.9bn next to €14.0bn, 4.0% next to 5.9%).
  const dec = (x: number) => roundHalfUp(x, 1).toLocaleString("en-US", { minimumFractionDigits: Math.abs(x) < 100 ? 1 : 0, maximumFractionDigits: 1 });
  if (unit === "%" || unit === "percent") return `${dec(n)}%`;
  const v = n * SCALE_MULT[scale];
  const abs = Math.abs(v);
  const [d, s] = abs >= 1e9 ? [1e9, "bn"] : abs >= 1e6 ? [1e6, "m"] : abs >= 1e4 ? [1e3, "k"] : [1, ""];
  const num = abs === 0 ? "0" : dec(abs / d);
  const sign = v < 0 ? "-" : "";
  const cur = CURRENCY[unit];
  return cur ? `${sign}${cur}${num}${s}` : `${sign}${num}${s} ${unit}`.trim();
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

export const human = (s: string) => s.replace(/_/g, " ");
