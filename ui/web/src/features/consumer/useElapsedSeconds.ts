// SPDX-License-Identifier: AGPL-3.0-only
import { useEffect, useState } from 'react';

/**
 * Whole seconds elapsed since a real client-observed instant, refreshed on a
 * one-second tick. Returns null while nothing is being waited on.
 *
 * This is the only "reconnecting (~N s)" number the page is allowed to show: it
 * measures the client's own wait. No contract publishes a cold-restart ETA, so
 * none is invented.
 */
export function useElapsedSeconds(
  since_unix_ms: number | null,
  now: () => number = Date.now,
): number | null {
  const [elapsed, setElapsed] = useState<number | null>(
    since_unix_ms === null ? null : Math.max(0, Math.round((now() - since_unix_ms) / 1000)),
  );

  useEffect(() => {
    if (since_unix_ms === null) {
      setElapsed(null);
      return;
    }
    const read = () => setElapsed(Math.max(0, Math.round((now() - since_unix_ms) / 1000)));
    read();
    const timer = setInterval(read, 1_000);
    return () => clearInterval(timer);
  }, [since_unix_ms, now]);

  return elapsed;
}
