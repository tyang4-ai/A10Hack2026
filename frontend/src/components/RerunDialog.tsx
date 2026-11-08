"use client";

// Rerun from a step. This forks the run: the new run reuses everything before the
// chosen step and redoes the rest with the analyst's instructions. The original
// run stays as it was, so the audit trail is never rewritten.
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { Stage } from "@contract/types";
import { NotSupported, runApi } from "@/lib/agentApi";
import { STAGE_LABEL } from "@/lib/format";

// What the new run keeps from this one when it reruns from each step.
export const RERUN_KEEPS: Partial<Record<Stage, string>> = {
  plan: "Starts from scratch.",
  approve: "Keeps the plan. You approve sources again.",
  collect: "Keeps your approvals. Fetches again.",
  extract: "Keeps the documents. Extracts again.",
  draft: "Keeps the verified data. Redrafts conclusions and scores.",
  verify: "Keeps the draft. Checks again.",
  repair: "Keeps the checks. Repairs again.",
  summarize: "Keeps the agent report. Rewrites the final report.",
  verify_summary: "Keeps the final report. Checks it again.",
};

const EXAMPLES: Partial<Record<Stage, string>> = {
  plan: "focus on leverage and liquidity, and skip news sources.",
  approve: "prefer primary filings over investor presentations.",
  collect: "also fetch the fiscal 2024 annual report.",
  extract: "take revenue from the income statement, not the press release.",
  draft: "be stricter on secular growth, and say why when data is thin.",
  verify: "treat rounding differences over 1% as failures.",
  repair: "downgrade rather than delete items you can't re-cite.",
  summarize: "lead with the leverage risk, and keep it to two pages.",
  verify_summary: "flag any sentence that only cites the scorecard.",
};

export function RerunDialog({ runId, stage, onClose }: { runId: string; stage: Stage; onClose: () => void }) {
  const router = useRouter();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [unsupported, setUnsupported] = useState(false);

  // Esc closes; focus returns to the step that opened the dialog. onClose is read
  // through a ref so the top bar re-rendering on every poll doesn't re-run this.
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const k = (e: KeyboardEvent) => e.key === "Escape" && closeRef.current();
    window.addEventListener("keydown", k);
    return () => {
      window.removeEventListener("keydown", k);
      prev?.focus?.();
    };
  }, []);

  const start = async () => {
    setBusy(true);
    setErr(null);
    try {
      const run = await runApi.rerun(runId, { from_stage: stage, instructions: text.trim() });
      onClose();
      router.push(`/runs/${encodeURIComponent(run.run_id)}`);
    } catch (e) {
      if (e instanceof NotSupported) setUnsupported(true);
      else setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const label = STAGE_LABEL[stage];
  return (
    <div className="fixed inset-0 z-[200000] flex items-start justify-center bg-black/40 px-4 pt-[14vh] print:hidden" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="rerun-title"
        onMouseDown={(e) => e.stopPropagation()}
        className="w-full max-w-[560px] border border-desk-line-strong bg-desk p-4 text-[13px] text-desk-ink"
      >
        <h2 id="rerun-title" className="text-[15px] font-semibold text-white">
          Rerun from {label}
        </h2>
        <p className="mt-1 text-desk-muted">
          Starts a new run from this one. {RERUN_KEEPS[stage]} This run stays exactly as it is, so you can compare the two.
        </p>
        <label htmlFor="rerun-text" className="mt-3 block text-[12px] font-medium text-desk-ink">
          Instructions for the agents from {label} on
        </label>
        <textarea
          id="rerun-text"
          autoFocus
          rows={4}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim()) void start();
          }}
          placeholder={EXAMPLES[stage]}
          className="mt-1 w-full resize-y rounded-[4px] border border-desk-line-strong bg-desk-raised px-2 py-1.5 text-desk-ink placeholder:text-desk-muted focus:border-accent"
        />
        {unsupported && (
          <div role="status" className="mt-2 rounded-[4px] border border-desk-warn/50 px-2.5 py-2 text-desk-warn">
            Rerun isn&apos;t available yet.
          </div>
        )}
        {err && (
          <div role="alert" className="mt-2 text-desk-bad">
            {err}
          </div>
        )}
        <div className="mt-3 flex items-center gap-2">
          <button
            onClick={() => void start()}
            disabled={busy || !text.trim() || unsupported}
            className="h-8 rounded-[4px] bg-accent px-3.5 font-semibold text-accent-ink disabled:opacity-40"
          >
            Start rerun
          </button>
          <button onClick={onClose} className="h-8 rounded-[4px] px-2.5 text-desk-muted hover:text-white">
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
