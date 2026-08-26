// SPDX-License-Identifier: AGPL-3.0-or-later
//
// TypeScript mirrors of the four frozen Mycelium A8 internet-native closed shapes.
//
// These types intentionally mirror `mycelium_internet/contracts.py` (read-only source
// of truth) so the UI can render projection-shaped inputs without ever expanding
// the vocabulary or importing Python. Nullable fields stay `number | null` to
// enforce the unknown-not-zero invariant; relays stay `hmac-sha256:` digests;
// endpoints stay `sha256:` pseudonyms.

export type FreshnessState = 'current' | 'stale' | 'unknown';
export type TlsState = 'publicly_trusted' | 'unverified' | 'unknown';
export type SeedPinState = 'verified' | 'mismatch' | 'unknown';
export type RouteState = 'available' | 'unavailable' | 'unknown';
export type InvitationState = 'pending' | 'accepted' | 'rejected' | 'unknown';
export type PathClass = 'direct' | 'relay' | 'unknown';
export type PathSource = 'bound_live_connection' | 'unknown';

export interface InternetBootstrapCounters {
  readonly requests: number;
  readonly joins_accepted: number;
  readonly joins_rejected: number;
}

export interface InternetBootstrapStatus {
  readonly protocol: 'mycelium.internet_bootstrap_status.v1';
  readonly generation: number;
  readonly observed_at_unix_ms: number;
  readonly freshness: FreshnessState;
  readonly tls_state: TlsState;
  readonly canonical_origin_verified: boolean;
  readonly seed_pin_state: SeedPinState;
  readonly route_state: RouteState;
  readonly invitation_state: InvitationState;
  readonly counters: InternetBootstrapCounters;
}

export interface InternetActivationMetrics {
  readonly rtt_ms: number | null;
  readonly warm_rtt_ms: number | null;
  readonly jitter_ms: number | null;
  readonly goodput_bytes_per_second: number | null;
  readonly loss_ratio: number | null;
  readonly sample_count: number | null;
  readonly measured_zero: boolean;
}

export interface InternetActivationObservation {
  readonly protocol: 'mycelium.internet_activation_observation.v1';
  readonly observation_id: string;
  readonly connection_generation: number;
  readonly connection_reuse: number;
  readonly path_class: PathClass;
  readonly path_source: PathSource;
  readonly endpoint_pseudonym: string | null;
  readonly observed_at_unix_ms: number;
  readonly freshness: FreshnessState;
  readonly evidence_lifetime_until_unix_ms: number;
  readonly metrics: InternetActivationMetrics;
}

export interface RelayProjection {
  readonly protocol: 'mycelium.relay_projection.v1';
  readonly relay_reference: string;
  readonly region: string;
  readonly projection_generation: number;
  readonly stable: boolean;
  readonly observed_at_unix_ms: number;
}

export type InternetNativeGateKind = 'physical_positive' | 'physical_negative';
export type InternetNativeQualificationResult = 'passed' | 'failed' | 'not_executed';

export interface InternetNativeQualificationPublicProjection {
  readonly gate_case_ids: readonly string[];
  readonly outcomes: readonly string[];
  readonly relay_reference: string | null;
  readonly observed_at_unix_ms: number;
}

export interface InternetNativeQualification {
  readonly protocol: 'mycelium.internet_native_qualification.v1';
  readonly qualification_id: string;
  readonly gate_kind: InternetNativeGateKind;
  readonly case_ids: readonly string[];
  readonly observed_at_unix_ms: number;
  readonly executed: boolean;
  readonly result: InternetNativeQualificationResult;
  readonly spec_digest: string;
  readonly source_digest: string;
  readonly evidence_digests: readonly string[];
  readonly fresh_until_unix_ms: number | null;
  readonly projection_digest: string | null;
  readonly public_projection: InternetNativeQualificationPublicProjection;
}

export interface InternetNativeSnapshot {
  readonly bootstrap_status: InternetBootstrapStatus;
  readonly activation_observation: InternetActivationObservation;
  readonly activation_history: readonly InternetActivationObservation[];
  readonly relay_projection: RelayProjection | null;
  readonly qualification: InternetNativeQualification | null;
}
