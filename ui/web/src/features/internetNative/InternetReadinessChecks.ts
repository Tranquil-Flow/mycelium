// SPDX-License-Identifier: AGPL-3.0-or-later
//
// InternetReadinessChecks — Readiness workspace pure checks.
//
// Nine separate readiness checks. A check is `ready` only from verified
// inputs; unknown inputs stay `unknown` (or `blocked` where an objective
// requires them) and never promote readiness.

import type {
  InternetActivationObservation,
  InternetBootstrapStatus,
} from './types';

export type ReadinessState = 'ready' | 'blocked' | 'unknown';

export interface InternetReadinessCheck {
  readonly check_id: string;
  readonly label: string;
  readonly state: ReadinessState;
  readonly reason: string | null;
}

export interface InternetReadinessExtras {
  readonly artifact_verified: boolean | null;
  readonly load_proven: boolean | null;
  readonly topology_bound: boolean | null;
  readonly qualifier_authority: boolean | null;
  readonly selection_authority: boolean | null;
}

function fromBoolean(value: boolean | null): ReadinessState {
  if (value === null) return 'unknown';
  return value ? 'ready' : 'blocked';
}

const CHECK_LABELS: Record<string, string> = {
  https: 'Public HTTPS bootstrap',
  seed_pin: 'Seed key pin',
  membership: 'Membership',
  activation_transport: 'Activation transport',
  artifact: 'Artifact',
  load: 'Load',
  topology: 'Topology',
  qualifier: 'Qualifier',
  selection: 'Selection',
};

export function buildInternetReadinessChecks(
  bootstrap: InternetBootstrapStatus | null,
  observation: InternetActivationObservation | null,
  extras: InternetReadinessExtras,
): InternetReadinessCheck[] {
  const httpsReady =
    bootstrap !== null &&
    bootstrap.tls_state === 'publicly_trusted' &&
    bootstrap.route_state === 'available';
  const pinReady = bootstrap !== null && bootstrap.seed_pin_state === 'verified';
  const membershipReady =
    bootstrap !== null && bootstrap.invitation_state === 'accepted';

  let activationState: ReadinessState = 'unknown';
  let activationReason: string | null = 'no_observation';
  if (observation !== null) {
    if (observation.path_class === 'unknown') {
      activationState = 'blocked';
      activationReason = 'unknown_path_class';
    } else if (observation.freshness === 'current') {
      activationState = 'ready';
      activationReason = null;
    } else {
      activationState = 'blocked';
      activationReason = 'stale_observation';
    }
  }

  const checks: Array<[string, ReadinessState, string | null]> = [
    ['https', httpsReady ? 'ready' : bootstrap === null ? 'unknown' : 'blocked', null],
    ['seed_pin', pinReady ? 'ready' : bootstrap === null ? 'unknown' : 'blocked', null],
    ['membership', membershipReady ? 'ready' : bootstrap === null ? 'unknown' : 'blocked', null],
    ['activation_transport', activationState, activationReason],
    ['artifact', fromBoolean(extras.artifact_verified), null],
    ['load', fromBoolean(extras.load_proven), null],
    ['topology', fromBoolean(extras.topology_bound), null],
    ['qualifier', fromBoolean(extras.qualifier_authority), null],
    ['selection', fromBoolean(extras.selection_authority), null],
  ];
  return checks.map(([check_id, state, reason]) => ({
    check_id,
    label: CHECK_LABELS[check_id],
    state,
    reason,
  }));
}
