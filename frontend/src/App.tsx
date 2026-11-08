import { useState } from "react";
import { api, usingMock } from "./api";
import { useRun } from "./useRun";
import { Pipeline } from "./components/Pipeline";
import { Traffic } from "./components/Traffic";

// Placeholder shell: starts a run against the API (or the mock) and shows the
// pipeline and gateway traffic. The real panels follow the design canvas.
export default function App() {
  const [runId, setRunId] = useState<string | null>(null);
  const [company, setCompany] = useState("Siemens Healthineers");
  const { run, events } = useRun(runId);

  if (!runId) {
    return (
      <main style={{ padding: 32, fontFamily: "serif" }}>
        <h1>Axelrod</h1>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            const r = await api.createRun(company);
            setRunId(r.id);
          }}
        >
          <label>
            Company <input value={company} onChange={(e) => setCompany(e.target.value)} />
          </label>{" "}
          <button type="submit">Start run</button>
          {usingMock && <p>Using the built-in mock backend.</p>}
        </form>
      </main>
    );
  }

  return (
    <main style={{ display: "flex", gap: 24, padding: 24, fontFamily: "serif" }}>
      {run && <Pipeline steps={run.steps} />}
      <section style={{ flex: 1 }}>
        <h1>
          {run?.company} <small>{run?.status}</small>
        </h1>
        {run?.status === "awaiting_approval" && (
          <button onClick={() => api.approveSources(runId)}>Approve sources</button>
        )}{" "}
        <button onClick={() => api.stopRun(runId, "stopped from dashboard")}>Stop run</button>
      </section>
      <Traffic events={events} />
    </main>
  );
}
