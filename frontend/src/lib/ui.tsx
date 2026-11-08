import type { ReactNode } from "react";
import type { Claim, CodeCheck, RunStatus, StepStatus, Tier, Verdict } from "@/lib/api";

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return <span className={`badge tone-${tone}`} title={title}>{children}</span>;
}

export type Tone = "neutral" | "good" | "warn" | "bad" | "info" | "accent";

export const TIER_LABEL: Record<Tier, string> = { 1: "Primary", 2: "Secondary", 3: "Low trust" };

export function TierBadge({ tier }: { tier: Tier }) {
  return <span className={`tier tier-${tier}`} title={TIER_LABEL[tier]}>T{tier}</span>;
}

export const runTone: Record<RunStatus, Tone> = {
  running: "info", awaiting_approval: "warn", completed: "good", stopped: "bad", failed: "bad",
};
export const runLabel: Record<RunStatus, string> = {
  running: "Running", awaiting_approval: "Waiting for approval", completed: "Completed", stopped: "Stopped", failed: "Failed",
};

export const stepTone: Record<StepStatus, Tone> = {
  pending: "neutral", running: "info", waiting: "warn", done: "good", failed: "bad", stopped: "bad",
};

export const codeTone: Record<CodeCheck, Tone> = {
  pending: "neutral", pass: "good", position_off: "warn", not_found: "bad",
  value_mismatch: "bad", tier_violation: "bad", source_removed: "bad",
};
export const codeLabel: Record<CodeCheck, string> = {
  pending: "Pending", pass: "Quote found", position_off: "Position off", not_found: "Not found",
  value_mismatch: "Value mismatch", tier_violation: "Tier violation", source_removed: "Source removed",
};

export const verdictTone: Record<Verdict, Tone> = {
  pending: "neutral", supported: "good", unsupported: "bad", misleading: "warn",
};

type Checked = { code_check: { status: CodeCheck }; agent_check: { verdict: Verdict }; resolution?: Claim["resolution"] };

// Failed verification (regardless of what a person did about it).
export const failedChecks = (c: Checked) =>
  ["not_found", "value_mismatch", "tier_violation", "source_removed"].includes(c.code_check.status) ||
  c.agent_check.verdict === "unsupported" || c.agent_check.verdict === "misleading";

export const isDropped = (c: Checked) => c.resolution?.action === "drop";
export const isWorking = (c: Checked) => c.resolution?.state === "working";

// A problem still waiting for a person: failed and not yet dropped or being fixed.
export const isFailed = (c: Checked) => failedChecks(c) && !isDropped(c) && !isWorking(c);

// Kept out of the report: failed, dropped, or being fixed.
export const isWithheld = (c: Checked) => failedChecks(c) || isDropped(c) || isWorking(c);

export const fmtTime = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
