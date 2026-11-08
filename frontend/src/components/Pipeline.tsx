import type { Step } from "@/lib/api";

const STAGES = { 1: "Sources", 2: "Initial report", 3: "Output" } as const;
const ACTOR_TITLE: Record<string, string> = { H: "Human", A: "Agent", C: "Code" };

export function Pipeline({ steps }: { steps: Step[] }) {
  return (
    <nav className="pipeline" aria-label="Pipeline">
      {([1, 2, 3] as const).map((stage) => (
        <section key={stage}>
          <h3>
            <span className="stage-num">{stage}</span> {STAGES[stage]}
          </h3>
          <ol>
            {steps.filter((s) => s.stage === stage).map((s) => (
              <li key={s.n} className={`step step-${s.status}`}>
                <span className="step-dot" aria-hidden />
                <span className="step-n">{s.n}</span>
                <span className="step-name">{s.name}</span>
                <span className="step-actor" title={s.actor.split("+").map((a) => ACTOR_TITLE[a]).join(" + ")}>
                  {s.actor}
                </span>
              </li>
            ))}
          </ol>
        </section>
      ))}
      <p className="legend">H human · A agent · C code</p>
    </nav>
  );
}
