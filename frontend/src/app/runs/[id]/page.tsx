import { Suspense } from "react";
import { Dashboard } from "@/components/Dashboard";

// One run's desk. The run id comes from the URL, so every run has its own link.
// With Cache Components on, reading params is runtime data and must sit inside Suspense.
export default function RunPage(props: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<div className="h-screen bg-desk" aria-busy="true" />}>
      <RunDesk params={props.params} />
    </Suspense>
  );
}

async function RunDesk({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <Dashboard runId={decodeURIComponent(id)} />;
}
