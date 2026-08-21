// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Pure render helpers for the four frozen Mycelium A8 internet-native shapes.
//
// Two invariants govern every helper below:
//
//   * unknown-not-zero — a nullable metric must render as the literal string
//     "unknown". It must NEVER render as "0" or any other numeric placeholder.
//   * privacy-safe relay projection — the only allowed relay value is the
//     stable `hmac-sha256:` reference. Raw URLs, IP addresses, port numbers,
//     DNS names, and credentials are rejected at the render boundary.
//
// Helpers are deliberately small, side-effect free, and friendly to component
// unit tests.

import type {
  FreshnessState,
  InternetActivationObservation,
  InternetBootstrapStatus,
  InvitationState,
  PathClass,
  RelayProjection,
  RouteState,
  SeedPinState,
  TlsState,
} from './types';

const PSEUDONYM_RE = /^sha256:[0-9a-f]{64}$/;
const RELAY_REFERENCE_RE = /^hmac-sha256:[0-9a-f]{64}$/;
const REGION_RE = /^[A-Za-z0-9 _-]{1,64}$/;

export class ProjectionError extends Error {
  constructor(code: string) {
    super(code);
    this.name = 'ProjectionError';
  }
}

/**
 * Render a nullable numeric metric. Null is the canonical "unknown" value and
 * must never appear as `0` (the unknown-not-zero invariant). Numbers are
 * rendered via `String(value)` so `0` still prints as `0` when explicitly
 * measured.
 */
export function renderMetric(value: number | null): string {
  if (value === null) return 'unknown';
  return String(value);
}

/** Render a path class exactly as the source vocabulary spells it. */
export function renderPathClass(value: PathClass): PathClass {
  switch (value) {
    case 'direct':
      return 'direct';
    case 'relay':
      return 'relay';
    case 'unknown':
      return 'unknown';
    default: {
      const exhaustive: never = value;
      throw new ProjectionError(`unknown_path_class:${String(exhaustive)}`);
    }
  }
}

/** Render a freshness value exactly as the source vocabulary spells it. */
export function renderFreshness(value: FreshnessState): FreshnessState {
  switch (value) {
    case 'current':
      return 'current';
    case 'stale':
      return 'stale';
    case 'unknown':
      return 'unknown';
    default: {
      const exhaustive: never = value;
      throw new ProjectionError(`unknown_freshness:${String(exhaustive)}`);
    }
  }
}

/**
 * Render a relay region. Coarse operator-declared regions pass through;
 * malformed strings are rejected so a UI consumer can never broadcast a raw
 * hostname, URL, or credential fragment under the "region" label.
 */
export function renderRelayRegion(value: string): string {
  if (value === 'unknown') return 'unknown';
  if (!REGION_RE.test(value)) {
    throw new ProjectionError('relay_region_invalid');
  }
  return value;
}

/**
 * Validate a relay reference. Accepts only `hmac-sha256:64hex`. Anything else
 * — including raw URLs, DNS names, port numbers, or a bare sha256 digest —
 * is rejected. The privacy-safe relay projection depends on this gate.
 */
export function validateRelayReference(value: string): boolean {
  return typeof value === 'string' && RELAY_REFERENCE_RE.test(value);
}

/**
 * Render a measured loss ratio as a percentage string. The null case is
 * `unknown`. The explicit zero case is accepted only when the source
 * observation declared `measured_zero === true` AND `sample_count >= 1`
 * (the explicit-zero rule). Any other zero, or any ratio without a current
 * sample, throws a bounded `ProjectionError`.
 */
export function renderLossRatio(
  value: number | null,
  sampleCount: number | null,
  measuredZero: boolean | null,
): string {
  if (value === null) return 'unknown';
  if (!Number.isFinite(value) || value < 0 || value > 1) {
    throw new ProjectionError('loss_ratio_out_of_range');
  }
  if (value === 0) {
    if (sampleCount === null || sampleCount < 1) {
      throw new ProjectionError('loss_ratio_zero_without_samples');
    }
    if (!measuredZero) {
      throw new ProjectionError('loss_ratio_zero_not_measured');
    }
    return '0';
  }
  if (sampleCount === null || sampleCount < 1) {
    throw new ProjectionError('loss_ratio_without_samples');
  }
  return `${(value * 100).toFixed(2)}%`;
}

/**
 * Render an endpoint pseudonym. Accepts only `sha256:64hex`. Null or any
 * other shape — including a raw endpoint id or a relay reference — renders
 * as `unknown` so the UI never exposes a network identity.
 */
export function renderPseudonym(value: string | null): string {
  if (value === null) return 'unknown';
  return PSEUDONYM_RE.test(value) ? value : 'unknown';
}

/**
 * Project a complete activation observation into render-ready strings. Null
 * metrics become `unknown`; the path class is rendered verbatim; the
 * pseudonym passes through `renderPseudonym`; the relay reference (when
 * present in the observation envelope) is validated, never rendered raw.
 */
export interface RenderedActivationObservation {
  readonly observation_id: string;
  readonly path_class: PathClass;
  readonly freshness: FreshnessState;
  readonly endpoint_pseudonym: string;
  readonly rtt_ms: string;
  readonly warm_rtt_ms: string;
  readonly jitter_ms: string;
  readonly goodput_bytes_per_second: string;
  readonly loss_ratio: string;
  readonly relay_reference: string | null;
}

export function renderActivationObservation(
  observation: InternetActivationObservation,
  relay: RelayProjection | null,
): RenderedActivationObservation {
  const metrics = observation.metrics;
  return Object.freeze({
    observation_id: observation.observation_id,
    path_class: renderPathClass(observation.path_class),
    freshness: renderFreshness(observation.freshness),
    endpoint_pseudonym: renderPseudonym(observation.endpoint_pseudonym),
    rtt_ms: renderMetric(metrics.rtt_ms),
    warm_rtt_ms: renderMetric(metrics.warm_rtt_ms),
    jitter_ms: renderMetric(metrics.jitter_ms),
    goodput_bytes_per_second: renderMetric(metrics.goodput_bytes_per_second),
    loss_ratio: renderLossRatio(metrics.loss_ratio, metrics.sample_count, metrics.measured_zero),
    relay_reference:
      relay === null
        ? null
        : validateRelayReference(relay.relay_reference)
          ? relay.relay_reference
          : null,
  });
}

/** Render a bootstrap status into display-ready labels. */
export interface RenderedBootstrapStatus {
  readonly freshness: FreshnessState;
  readonly tls_state: TlsState;
  readonly canonical_origin_verified: 'verified' | 'unverified' | 'unknown';
  readonly seed_pin_state: SeedPinState;
  readonly route_state: RouteState;
  readonly invitation_state: InvitationState;
  readonly counters: {
    readonly requests: string;
    readonly joins_accepted: string;
    readonly joins_rejected: string;
  };
}

export function renderBootstrapStatus(
  status: InternetBootstrapStatus | null,
): RenderedBootstrapStatus {
  if (status === null) {
    return Object.freeze({
      freshness: 'unknown',
      tls_state: 'unknown',
      canonical_origin_verified: 'unknown',
      seed_pin_state: 'unknown',
      route_state: 'unknown',
      invitation_state: 'unknown',
      counters: Object.freeze({
        requests: 'unknown',
        joins_accepted: 'unknown',
        joins_rejected: 'unknown',
      }),
    });
  }
  return Object.freeze({
    freshness: renderFreshness(status.freshness),
    tls_state: status.tls_state,
    canonical_origin_verified: status.canonical_origin_verified ? 'verified' : 'unverified',
    seed_pin_state: status.seed_pin_state,
    route_state: status.route_state,
    invitation_state: status.invitation_state,
    counters: Object.freeze({
      requests: String(status.counters.requests),
      joins_accepted: String(status.counters.joins_accepted),
      joins_rejected: String(status.counters.joins_rejected),
    }),
  });
}