// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from 'vitest';
import { planRequiresPathCosts } from './PlansInternetGate';
import type { InternetActivationObservation } from './types';

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

describe('PlansInternetGate (Plans workspace)', () => {
  it('allows an objective that needs path costs on a measured direct path', () => {
    expect(planRequiresPathCosts(observation())).toEqual({
      blocked: false,
      reason: null,
    });
  });

  it('blocks when there is no observation at all', () => {
    expect(planRequiresPathCosts(null)).toEqual({
      blocked: true,
      reason: 'no_observation',
    });
  });

  it('blocks when the path class is unknown', () => {
    expect(
      planRequiresPathCosts(
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
      ),
    ).toEqual({ blocked: true, reason: 'unknown_path_class' });
  });

  it('blocks when the required metrics are missing', () => {
    expect(
      planRequiresPathCosts(
        observation({
          metrics: {
            rtt_ms: null,
            warm_rtt_ms: 9,
            jitter_ms: 2,
            goodput_bytes_per_second: null,
            loss_ratio: null,
            sample_count: null,
            measured_zero: false,
          },
        }),
      ),
    ).toEqual({ blocked: true, reason: 'missing_path_metrics' });
  });

  it('never treats a null metric as zero', () => {
    const verdict = planRequiresPathCosts(
      observation({
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
    );
    expect(verdict.blocked).toBe(true);
  });
});
