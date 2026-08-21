// SPDX-License-Identifier: AGPL-3.0-or-later
//
// PlansInternetGate — Plans workspace pure gate.
//
// Plan objectives that require measured path costs stay BLOCKED while the
// path class or the required metrics are unknown. Unknown inputs are never
// coerced to zero and never satisfy an objective.

import type { InternetActivationObservation } from './types';

export interface PathCostVerdict {
  readonly blocked: boolean;
  readonly reason:
    | 'no_observation'
    | 'unknown_path_class'
    | 'missing_path_metrics'
    | null;
}

export function planRequiresPathCosts(
  observation: InternetActivationObservation | null,
): PathCostVerdict {
  if (observation === null) {
    return { blocked: true, reason: 'no_observation' };
  }
  if (observation.path_class === 'unknown') {
    return { blocked: true, reason: 'unknown_path_class' };
  }
  const metrics = observation.metrics;
  if (metrics.rtt_ms === null || metrics.goodput_bytes_per_second === null) {
    return { blocked: true, reason: 'missing_path_metrics' };
  }
  return { blocked: false, reason: null };
}
