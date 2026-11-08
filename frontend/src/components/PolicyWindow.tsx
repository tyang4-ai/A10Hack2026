"use client";

// The gateway's allowlist as rows. While the analyst is deciding, a preview built
// from the draft decisions shows what the gateway will allow once they approve.
import { useEffect, useMemo, useState } from "react";
import type { PolicyRule, Source } from "@contract/types";
import { draftDecision, useRunStore } from "@/lib/store";

const EFFECT_CLS = { allow: "text-desk-good", deny: "text-desk-bad", wait: "text-desk-warn" } as const;

function pathOf(url: string): string {
  try {
    const p = new URL(url).pathname;
    return p === "/" ? "" : p;
  } catch {
    return "";
  }
}

function ruleTarget(r: PolicyRule): string {
  if (r.action === "model.invoke") return r.model_id ?? "any model";
  return `${r.domain ?? "any domain"}${r.path_prefix ?? ""}`;
}

function Row({ effect, id, action, target, note }: { effect: keyof typeof EFFECT_CLS; id: string; action: string; target: string; note?: string | null }) {
  return (
    <tr className="border-t border-desk-line align-top">
      <td className={`px-3 py-1.5 font-mono text-[11px] font-semibold ${EFFECT_CLS[effect]}`}>{effect === "wait" ? "BLOCKED" : effect.toUpperCase()}</td>
      <td className="px-2 py-1.5 font-mono whitespace-nowrap text-desk-muted">{id}</td>
      <td className="px-2 py-1.5 font-mono whitespace-nowrap text-desk-muted">{action}</td>
      <td className="px-2 py-1.5">
        <span className="font-mono break-all">{target}</span>
        {note && <div className="text-desk-muted">{note}</div>}
      </td>
    </tr>
  );
}

function Head({ children }: { children: React.ReactNode }) {
  return (
    <tr>
      <th colSpan={4} className="bg-desk px-3 pt-3 pb-1 text-left text-[12px] font-semibold text-desk-ink">
        {children}
      </th>
    </tr>
  );
}

export function PolicyWindow() {
  const runId = useRunStore((s) => s.runId);
  const policy = useRunStore((s) => s.policy);
  const awaiting = useRunStore((s) => s.run?.status === "awaiting_approval");
  const sourcesMap = useRunStore((s) => s.sources);
  const drafts = useRunStore((s) => s.drafts);
  const refreshPolicy = useRunStore((s) => s.refreshPolicy);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!runId || useRunStore.getState().policy) return;
    refreshPolicy().catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }, [runId, refreshPolicy]);

  const preview = useMemo(() => {
    if (!awaiting) return [];
    const list = Object.values(sourcesMap).sort((a, b) => a.tier.localeCompare(b.tier) || a.rank - b.rank);
    return list.map((s): [Source, ReturnType<typeof draftDecision>] => [s, draftDecision(s, drafts, true)]);
  }, [awaiting, sourcesMap, drafts]);
  const waiting = preview.filter(([, d]) => d === null).length;

  return (
    <div className="text-[12px] text-desk-ink">
      {err && (
        <p role="alert" className="px-3 pt-2 text-desk-bad">
          {err}
        </p>
      )}
      {policy?.kill_switch && (
        <p role="status" className="mx-3 mt-2 rounded-[4px] bg-desk-bad px-2.5 py-1.5 font-semibold text-desk">
          Run stopped. Every request is blocked.
        </p>
      )}
      <table className="mt-1 w-full border-collapse">
        <thead className="sr-only">
          <tr>
            <th>Effect</th>
            <th>Rule</th>
            <th>Action</th>
            <th>Target</th>
          </tr>
        </thead>
        <tbody>
          {awaiting && (
            <>
              <Head>
                Preview from your decisions{" "}
                <span className="font-normal text-desk-muted">{waiting ? `${waiting} still undecided` : "all decided"}</span>
              </Head>
              {preview.map(([s, d]) => (
                <Row
                  key={s.source_id}
                  effect={d === "approved" ? "allow" : d === "rejected" ? "deny" : "wait"}
                  id={s.source_id}
                  action="fetch"
                  target={`${s.domain}${pathOf(s.url)}`}
                  note={d === null ? "Undecided" : d === "rejected" ? "Rejected" : s.tier === "priority" ? null : "Auto-approved"}
                />
              ))}
            </>
          )}
          <Head>
            In force{" "}
            <span className="font-normal text-desk-muted">
              {policy ? `version ${policy.version}, ${policy.rules.length} rules` : "loading"}
            </span>
          </Head>
          {policy?.rules.map((r) => (
            <Row key={r.rule_id} effect={r.effect} id={r.rule_id} action={r.action} target={ruleTarget(r)} note={r.note ?? (r.roles.length ? `For ${r.roles.join(", ")}` : null)} />
          ))}
          {policy && policy.rules.length === 0 && (
            <tr>
              <td colSpan={4} className="px-3 py-1.5 text-desk-muted">
                No rules yet.
              </td>
            </tr>
          )}
          <Row effect="deny" id="default" action="any" target="Everything else" />
        </tbody>
      </table>
    </div>
  );
}
