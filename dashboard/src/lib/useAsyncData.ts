// The discriminated-union load-state idiom from the original App.tsx, made
// reusable. Every data fetch renders a loading, an error, or a success view.
import { useEffect, useState } from "react";

export type LoadState<T> =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "success"; data: T };

export interface AsyncData<T> {
  state: LoadState<T>;
  // Re-run the loader (used after a create / delete / revoke mutation).
  reload: () => void;
}

/**
 * Run ``load`` on mount and whenever ``deps`` change (or ``reload`` is called),
 * exposing the result as a LoadState. Stale results from a superseded run are
 * discarded via the cancellation flag.
 */
export function useAsyncData<T>(
  load: () => Promise<T>,
  deps: readonly unknown[],
): AsyncData<T> {
  const [state, setState] = useState<LoadState<T>>({ kind: "loading" });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState({ kind: "loading" });
    load().then(
      (data) => {
        if (!cancelled) {
          setState({ kind: "success", data });
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          const message =
            error instanceof Error ? error.message : "Something went wrong.";
          setState({ kind: "error", message });
        }
      },
    );
    return () => {
      cancelled = true;
    };
    // ``load`` is intentionally excluded: it is a fresh closure each render, so
    // depending on it would re-fetch on every render. We re-run on deps/nonce.
  }, [...deps, nonce]);

  return { state, reload: () => setNonce((n) => n + 1) };
}
