"use client";

import { useState } from "react";
import { api, type RunDetail, type RunEvent, type Source } from "@/lib/api";
import { btn, Footnote, PageHead, Section, StatRow } from "./Shell";

const TYPE: [RegExp, string][] = [
  [/annual report/i, "FILING"],
  [/release/i, "RELEASE"],
  [/transcript/i, "TRANSCRIPT"],
  [/presentation|capital markets/i, "DECK"],
  [/exchange|listing/i, "EXCHANGE"],
];
const typeOf = (s: Source) => TYPE.find(([re]) => re.test(s.title))?.[1] ?? "WEB";

export function SourcesPanel({
  run,
  sources,
  events,
  refresh,
}: {
  run: RunDetail;
  sources: Source[];
  events: RunEvent[];
  refresh: () => Promise<void>;
}) {
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const live = sources.filter((s) => s.status !== "removed");
  const t1 = live.filter((s) => s.tier === 1);
  const auto = live.filter((s) => s.tier !== 1);
  const waiting = run.status === "awaiting_approval";
  const blocked = events.filter((e) => e.decision === "deny" && e.step <= 5);
  const act = async (p: Promise<unknown>) => {
    await p;
    await refresh();
  };
  const move = (i: number, d: number) => {
    const ids = t1.map((s) => s.id);
    const j = i + d;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    act(api.reorderSources(run.id, ids));
  };
  const tiers = [1, 2, 3].map((t) => live.filter((s) => s.tier === t).length);
  const total = Math.max(1, live.length);

  return (
    <div className="animate-[fade-in_200ms_ease-out]">
      <PageHead over={`STEP 04 / 12 · RANK & APPROVE SOURCES`} title="Source Approval" run={run}>
        <strong>Abstract.</strong> {live.length} sources proposed. {t1.length} primary sources need your order and approval; the order decides
        which source wins a disagreement. {auto.length} were auto-approved with limits: tier 2 figures must agree with tier 1, tier 3 never backs a
        figure alone.
      </PageHead>

      <div className="mt-5">
        <StatRow
          stats={[
            { label: "PROPOSED", value: live.length },
            { label: "NEED APPROVAL", value: t1.filter((s) => s.status === "proposed").length },
            { label: "AUTO-APPROVED", value: auto.length },
            { label: "BLOCKED", value: blocked.length, bad: blocked.length > 0 },
          ]}
        />
      </div>

      <figure className="mt-3.5">
        <div role="img" aria-label={`Source mix: ${tiers[0]} tier 1, ${tiers[1]} tier 2, ${tiers[2]} tier 3`} className="flex h-[18px] gap-0.5">
          <div className="bg-ink transition-[flex-grow] duration-500" style={{ flexGrow: tiers[0] / total }} />
          <div className="bg-[#8C8C8C] transition-[flex-grow] duration-500" style={{ flexGrow: tiers[1] / total }} />
          <div className="border border-dashed border-ink transition-[flex-grow] duration-500" style={{ flexGrow: tiers[2] / total }} />
        </div>
        <figcaption className="mt-1 flex flex-wrap gap-[18px] font-mono text-[11px]">
          <span>■ T1 PRIMARY {tiers[0]}</span>
          <span className="text-muted">■ T2 SECONDARY {tiers[1]}</span>
          <span>▢ T3 LOW TRUST {tiers[2]}</span>
          <span className="ml-auto font-serif text-[13px]">
            <strong>Figure 1.</strong> Source mix by tier.
          </span>
        </figcaption>
      </figure>

      <Section n="01" title="Primary sources">
        <div className="overflow-x-auto">
          <table className="w-full border-y border-ink font-mono text-xs">
            <thead>
              <tr className="border-b border-ink text-left text-[10px] tracking-[0.1em] text-muted">
                <th className="py-1.5 pr-2 text-right font-medium">#</th>
                <th className="px-2 font-medium">ID</th>
                <th className="px-2 font-medium">SOURCE</th>
                <th className="px-2 font-medium">DOMAIN</th>
                <th className="px-2 font-medium">TYPE</th>
                <th className="px-2 font-medium">STATUS</th>
                <th className="pl-2 text-right font-medium">ORDER</th>
              </tr>
            </thead>
            <tbody>
              {t1.map((s, i) => (
                <tr key={s.id} className="animate-[fade-in_250ms_ease-out] even:bg-zebra">
                  <td className="py-1 pr-2 text-right font-bold">{s.priority ?? i + 1}</td>
                  <td className="px-2">{s.id}</td>
                  <td className="px-2 font-serif text-[15px]">{s.title}</td>
                  <td className="px-2">{s.domain}</td>
                  <td className="px-2 text-muted">{typeOf(s)}</td>
                  <td className={`px-2 ${s.status === "fetched" ? "text-good" : s.status === "fetch_failed" ? "text-bad" : ""}`}>{s.status.toUpperCase()}</td>
                  <td className="py-1 pl-2 text-right whitespace-nowrap">
                    <button aria-label={`Move ${s.id} up`} disabled={!waiting || i === 0} onClick={() => move(i, -1)} className={`${btn} h-[26px] w-[26px] px-0`}>
                      ↑
                    </button>{" "}
                    <button aria-label={`Move ${s.id} down`} disabled={!waiting || i === t1.length - 1} onClick={() => move(i, 1)} className={`${btn} h-[26px] w-[26px] px-0`}>
                      ↓
                    </button>{" "}
                    <button aria-label={`Remove ${s.id}`} onClick={() => act(api.removeSource(run.id, s.id))} className={`${btn} h-[26px] w-[26px] px-0`}>
                      ✕
                    </button>
                  </td>
                </tr>
              ))}
              {t1.length === 0 && (
                <tr>
                  <td colSpan={7} className="py-3 text-muted">
                    Waiting for the agent to propose sources…
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <form
          className="mt-3 flex flex-wrap items-end gap-3.5 font-mono text-xs"
          onSubmit={(e) => {
            e.preventDefault();
            if (!url) return;
            act(api.addSource(run.id, url, title)).then(() => {
              setUrl("");
              setTitle("");
            });
          }}
        >
          <label className="flex flex-[2_1_260px] flex-col text-[10px] tracking-[0.1em] text-muted">
            ADD SOURCE URL
            <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" className="border-b border-ink bg-transparent py-1 text-[13px] tracking-normal text-ink outline-none focus:border-b-2" />
          </label>
          <label className="flex flex-[1_1_160px] flex-col text-[10px] tracking-[0.1em] text-muted">
            TITLE
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="optional" className="border-b border-ink bg-transparent py-1 text-[13px] tracking-normal text-ink outline-none focus:border-b-2" />
          </label>
          <button type="submit" className={`${btn} min-h-8 font-bold`}>
            + ADD
          </button>
          <button
            type="button"
            disabled={!waiting}
            onClick={() => act(api.approveSources(run.id))}
            className="ml-auto min-h-10 border border-ink bg-ink px-[18px] font-bold tracking-[0.08em] text-white transition-opacity disabled:opacity-30"
          >
            {waiting ? `APPROVE ${t1.length} & CONTINUE` : run.current_step > 4 ? "APPROVED ✓" : "WAITING FOR PROPOSALS"}
          </button>
        </form>
      </Section>

      <Section n="02" title="Auto-approved">
        <div className="overflow-x-auto">
          <table className="w-full border-y border-ink font-mono text-xs">
            <thead>
              <tr className="border-b border-ink text-left text-[10px] tracking-[0.1em] text-muted">
                <th className="py-1.5 pr-2 font-medium">TIER</th>
                <th className="px-2 font-medium">ID</th>
                <th className="px-2 font-medium">SOURCE</th>
                <th className="px-2 font-medium">MAY SUPPORT</th>
                <th className="pl-2" />
              </tr>
            </thead>
            <tbody>
              {auto.map((s) => (
                <tr key={s.id} className="animate-[fade-in_250ms_ease-out] even:bg-zebra">
                  <td className="py-1 pr-2 font-bold">T{s.tier}</td>
                  <td className="px-2">{s.id}</td>
                  <td className="px-2">
                    <span className="font-serif text-[15px]">{s.title}</span> <span className="text-muted">{s.domain}</span>
                  </td>
                  <td className="px-2 text-muted">{s.tier === 2 ? "FIGURES = T1" : "SENTIMENT ONLY"}</td>
                  <td className="py-1 pl-2 text-right whitespace-nowrap">
                    <button onClick={() => act(api.setSourceTier(run.id, s.id, 1))} className={`${btn} h-[26px]`}>
                      PROMOTE
                    </button>{" "}
                    <button aria-label={`Remove ${s.id}`} onClick={() => act(api.removeSource(run.id, s.id))} className={`${btn} h-[26px] w-[26px] px-0`}>
                      ✕
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      {blocked.map((e) => (
        <p key={e.seq} className="mt-4 animate-[fade-in_250ms_ease-out] text-justify">
          <strong>
            <em>Remark.</em>
          </strong>{" "}
          <span className="text-bad">Blocked</span>: {e.summary}
        </p>
      ))}
      <Footnote />
    </div>
  );
}
