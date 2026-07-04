import { useEffect, useState } from "react";

interface HealthResponse {
  status: string;
  database: boolean;
  redis: boolean;
}

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "success"; data: HealthResponse };

export function App() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const response = await fetch("/api/health");
        if (!response.ok) {
          throw new Error(`The status check returned ${response.status}.`);
        }
        const data = (await response.json()) as HealthResponse;
        if (!cancelled) {
          setState({ kind: "success", data });
        }
      } catch (error: unknown) {
        if (!cancelled) {
          const message =
            error instanceof Error ? error.message : "Unknown error.";
          setState({ kind: "error", message });
        }
      }
    }

    void load();

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main>
      <h1>Crashlens</h1>
      {state.kind === "loading" && <p>Checking system status...</p>}
      {state.kind === "error" && (
        <p role="alert">Could not reach the service: {state.message}</p>
      )}
      {state.kind === "success" && (
        <section aria-label="System status">
          <p>Overall status: {state.data.status}</p>
          <ul>
            <li>Database reachable: {state.data.database ? "yes" : "no"}</li>
            <li>Queue reachable: {state.data.redis ? "yes" : "no"}</li>
          </ul>
        </section>
      )}
    </main>
  );
}
