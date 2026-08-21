// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from 'vitest';
import { buildInternetReadinessChecks } from './InternetReadinessChecks';
import type {
  InternetActivationObservation,
  InternetBootstrapStatus,
} from './types';

function bootstrap(): InternetBootstrapStatus {
  return {
    protocol: 'mycelium.internet_bootstrap_status.v1',
    generation: 1,
    observed_at_unix_ms: 1752500000000,
    freshness: 'current',
    tls_state: 'publicly_trusted',
    canonical_origin_verified: true,
    seed_pin_state: 'verified',
    route_state: 'available',
    invitation_state: 'accepted',
    counters: { requests: 1, joins_accepted: 1, joins_rejected: 0 },
  };
}

function observation(
  overrides: Partial<InternetActivationObservation> = {},
): InternetActivationObservation {
  return {
    protocol: 'mycelium.internet_activation_observation.v1',
    observation_id: 'obs-1',
    connection_generation: 1,
    connection_reuse: 0,
    path_class: 'direct',
    path_source: 'bound_live_connection',
    endpoint_pseudonym: 'sha256:' + 'a'.repeat(64),
    observed_at_unix_ms: 1752500000000,
    freshness: 'current',
    evidence_lifetime_until_unix_ms: 1752500090000,
    metrics: {
      rtt_ms: 12,
      warm_rtt_ms: 9,
      jitter_ms: 2,
      goodput_bytes_per_second: 1500000,
      loss_ratio: 0,
      sample_count: 64,
      measured_zero: true,
    },
    ...overrides,
  };
}

const EXTRAS = {
  artifact_verified: true,
  load_proven: true,
  topology_bound: true,
  qualifier_authority: true,
  selection_authority: true,
};

describe('InternetReadinessChecks (Readiness workspace)', () => {
  it('emits nine separate checks with distinct labels', () => {
    const checks = buildInternetReadinessChecks(bootstrap(), observation(), EXTRAS);
    expect(checks).toHaveLength(9);
    expect(new Set(checks.map((check) => check.check_id)).size).toBe(9);
    expect(checks.map((check) => check.check_id)).toEqual([
      'https',
      'seed_pin',
      'membership',
      'activation_transport',
      'artifact',
      'load',
      'topology',
      'qualifier',
      'selection',
    ]);
  });

  it('marks https and pin ready from a verified bootstrap status', () => {
    const checks = buildInternetReadinessChecks(bootstrap(), observation(), EXTRAS);
    const byId = new Map(checks.map((check) => [check.check_id, check]));
    expect(byId.get('https')?.state).toBe('ready');
    expect(byId.get('seed_pin')?.state).toBe('ready');
    expect(byId.get('membership')?.state).toBe('ready');
  });

  it('never reports ready from unknown inputs', () => {
    const checks = buildInternetReadinessChecks(null, null, {
      artifact_verified: null,
      load_proven: null,
      topology_bound: null,
      qualifier_authority: null,
      selection_authority: null,
    });
    for (const check of checks) {
      expect(check.state).not.toBe('ready');
    }
    const activation = checks.find((check) => check.check_id === 'activation_transport');
    expect(activation?.state).toBe('unknown');
    expect(activation?.reason).toBe('no_observation');
  });

  it('blocks activation transport on an unknown path', () => {
    const checks = buildInternetReadinessChecks(
      bootstrap(),
      observation({
        path_class: 'unknown',
        path_source: 'unknown',
        metrics: {
          rtt_ms: null,
          warm_rtt_ms: null,
          jitter_ms: null,
          goodput_bytes_per_second: null,
          loss_ratio: null,
          sample_count: null,
          measured_zero: false,
        },
      }),
      EXTRAS,
    );
    const activation = checks.find((check) => check.check_id === 'activation_transport');
    expect(activation?.state).toBe('blocked');
    expect(activation?.reason).toBe('unknown_path_class');
  });

  it('blocks downstream checks when their authority is absent', () => {
    const checks = buildInternetReadinessChecks(bootstrap(), observation(), {
      ...EXTRAS,
      qualifier_authority: false,
      selection_authority: null,
    });
    const byId = new Map(checks.map((check) => [check.check_id, check]));
    expect(byId.get('qualifier')?.state).toBe('blocked');
    expect(byId.get('selection')?.state).toBe('unknown');
  });
});
