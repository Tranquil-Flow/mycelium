// SPDX-License-Identifier: AGPL-3.0-only
import type { ProductSnapshotMode } from '../productEvidence/contracts';

export const dimensions = {
  edge_kind: ['logical_execution', 'physical_membership', 'transport_observation'],
  phase: ['prefill_forward', 'decode_forward', 'decode_closure', 'control_probe', 'unknown'],
  path_status: ['proposed', 'selected', 'observed', 'failed', 'recovered', 'historical', 'unknown'],
  role: ['primary', 'alternative', 'replica', 'recovery_predecessor', 'recovery_successor', 'draft', 'unknown'],
  transport: ['direct', 'relay', 'unknown'],
  freshness: ['current', 'stale', 'unknown'],
} as const;
export type Dimension = keyof typeof dimensions;
export const regionCodes = ['africa', 'americas_north', 'americas_south', 'asia', 'europe', 'oceania', 'polar', 'unknown'] as const;
export type RegionCode = (typeof regionCodes)[number];
export const regionSourceKinds = ['unavailable', 'owner_declared', 'reviewed_relay_metadata', 'latency_distance_class'] as const;
export type RegionSourceKind = (typeof regionSourceKinds)[number];
export const regionBlockers = ['missing', 'expired', 'conflicting', 'unauthorized'] as const;
export type RegionBlocker = (typeof regionBlockers)[number];
export const distanceClasses = ['near', 'mid', 'far', 'unknown'] as const;
export type DistanceClass = (typeof distanceClasses)[number];
export const evidenceSourceKinds = ['owner_declared', 'reviewed_relay_metadata', 'latency_distance_class'] as const;
export type EvidenceSourceKind = (typeof evidenceSourceKinds)[number];
export const evidenceUncertainty: Record<EvidenceSourceKind, 'declared_unverified' | 'reviewed' | 'model_estimate'> = {
  owner_declared: 'declared_unverified',
  reviewed_relay_metadata: 'reviewed',
  latency_distance_class: 'model_estimate',
};
export interface RouteNode { readonly id: string; readonly kind: 'member' | 'endpoint'; readonly region: RegionCode; readonly region_source: RegionSourceKind }
export interface RegionRow { readonly node: string; readonly region: RegionCode; readonly region_source: RegionSourceKind; readonly region_blocker: RegionBlocker | null; readonly distance_class: DistanceClass | null }
export interface CoarseRegionEvidence {
  readonly protocol: 'mycelium.coarse_region_evidence.v1';
  readonly subject_ref: string;
  readonly subject_kind: 'member' | 'relay';
  readonly source_kind: EvidenceSourceKind;
  readonly region: RegionCode;
  readonly distance_class: DistanceClass | null;
  readonly vocabulary_version: 'mycelium.coarse_region.v1';
  readonly model_version: string | null;
  readonly uncertainty: 'declared_unverified' | 'reviewed' | 'model_estimate';
  readonly issued_at_unix_ms: number;
  readonly expires_at_unix_ms: number;
  readonly authority_digest: string;
  readonly digest: string;
}
export type RouteSegment = { readonly [K in Dimension]: (typeof dimensions)[K][number] } & {
  readonly id: string; readonly source: string | null; readonly destination: string | null;
  readonly binding: { readonly deployment: string | null; readonly request: string | null; readonly attempt: string | null; readonly track: string | null; readonly path_generation: number | null };
  readonly connection_generation: number | null; readonly issued_at: number | null; readonly expires_at: number | null;
  readonly metrics: { readonly rtt_ms: number | null; readonly warm_rtt_ms: number | null; readonly jitter_ms: number | null; readonly goodput_bytes_per_second: number | null; readonly loss_ratio: number | null; readonly sample_count: number | null; readonly measured_zero: boolean };
  readonly authority_digest: string; readonly provenance: ProductSnapshotMode; readonly history: boolean;
};
export interface RouteExplorer {
  readonly protocol: 'mycelium.route_explorer.v1'; readonly generation: number; readonly source_mode: ProductSnapshotMode;
  readonly source_digest: string; readonly digest: string; readonly overlays: 'unavailable';
  readonly nodes: readonly RouteNode[]; readonly segments: readonly RouteSegment[];
  readonly regions?: readonly RegionRow[];
}
const hash = (v: unknown) => typeof v === 'string' && /^sha256:[0-9a-f]{64}$/.test(v);
const integer = (v: unknown) => Number.isSafeInteger(v) && (v as number) >= 0;
function requireShape(ok: unknown): asserts ok { if (!ok) throw new TypeError('route_explorer_invalid'); }
function exact(v: unknown, keys: string[]): Record<string, unknown> {
  requireShape(typeof v === 'object' && v !== null && !Array.isArray(v));
  const r = v as Record<string, unknown>;
  requireShape(Object.keys(r).length === keys.length && keys.every(k => Object.hasOwn(r, k)));
  return r;
}
export function decodeRouteExplorer(value: unknown, generation: number, mode: ProductSnapshotMode): RouteExplorer {
  // The exact A14a eight-key shape, widened only by the optional A14b `regions` key.
  const r = exact(value, [
    'protocol', 'generation', 'source_mode', 'source_digest', 'digest', 'overlays', 'nodes', 'segments',
    ...(typeof value === 'object' && value !== null && !Array.isArray(value) && Object.hasOwn(value, 'regions') ? ['regions'] : []),
  ]);
  requireShape(r.protocol === 'mycelium.route_explorer.v1' && r.generation === generation && r.source_mode === mode);
  requireShape(hash(r.source_digest) && hash(r.digest) && r.overlays === 'unavailable');
  requireShape(Array.isArray(r.nodes) && r.nodes.length <= 4096 && Array.isArray(r.segments) && r.segments.length <= 4096);
  const ids = new Set<unknown>();
  let lastNode = '';
  for (const v of r.nodes) {
    const n = exact(v, ['id', 'kind', 'region', 'region_source']);
    requireShape(hash(n.id) && !ids.has(n.id) && (n.id as string) > lastNode);
    requireShape(['member', 'endpoint'].includes(n.kind as string) && (regionCodes as readonly unknown[]).includes(n.region) && (regionSourceKinds as readonly unknown[]).includes(n.region_source));
    ids.add(n.id); lastNode = n.id as string;
  }
  const seen = new Set<unknown>();
  let lastTime = -1; let lastId = '';
  for (const v of r.segments) {
    const s = exact(v, ['id', 'source', 'destination', 'binding', 'connection_generation', 'issued_at', 'expires_at', 'metrics', 'authority_digest', 'provenance', 'history', ...Object.keys(dimensions)]);
    requireShape(hash(s.id) && !seen.has(s.id) && hash(s.authority_digest)); seen.add(s.id);
    requireShape(s.provenance === mode && typeof s.history === 'boolean');
    requireShape([s.source, s.destination].every(x => x === null || ids.has(x)));
    for (const [k, values] of Object.entries(dimensions)) requireShape((values as readonly unknown[]).includes(s[k]));
    for (const k of ['connection_generation', 'issued_at', 'expires_at']) requireShape(s[k] === null || integer(s[k]));
    requireShape(s.issued_at === null || s.expires_at === null || (s.expires_at as number) >= (s.issued_at as number));
    const time = (s.issued_at as number | null) ?? 0;
    requireShape(time > lastTime || (time === lastTime && (s.id as string) > lastId)); lastTime = time; lastId = s.id as string;
    const b = exact(s.binding, ['deployment', 'request', 'attempt', 'track', 'path_generation']);
    for (const k of ['deployment', 'request', 'attempt', 'track']) requireShape(b[k] === null || hash(b[k]));
    requireShape(b.path_generation === null || integer(b.path_generation));
    const m = exact(s.metrics, ['rtt_ms', 'warm_rtt_ms', 'jitter_ms', 'goodput_bytes_per_second', 'loss_ratio', 'sample_count', 'measured_zero']);
    requireShape(typeof m.measured_zero === 'boolean');
    for (const k of Object.keys(m).filter(k => k !== 'measured_zero')) requireShape(m[k] === null || (typeof m[k] === 'number' && Number.isFinite(m[k]) && (m[k] as number) >= 0 && (m[k] as number) <= Number.MAX_SAFE_INTEGER));
    requireShape(m.sample_count === null || integer(m.sample_count));
    requireShape(m.loss_ratio === null || (m.loss_ratio as number) <= 1);
    requireShape(m.loss_ratio !== 0 || (m.measured_zero && (m.sample_count as number) > 0));
    if (s.transport === 'unknown') requireShape(Object.entries(m).every(([k,v]) => k === 'measured_zero' ? v === false : v === null));
    else requireShape(s.edge_kind === 'transport_observation' && s.destination !== null && s.connection_generation !== null && s.issued_at !== null && s.expires_at !== null);
  }
  if (Object.hasOwn(r, 'regions')) {
    const regionRows = r.regions;
    requireShape(Array.isArray(regionRows) && regionRows.length <= 4096 && regionRows.length === r.nodes.length);
    const rows = new Map<unknown, Record<string, unknown>>();
    let lastRowNode = '';
    for (const v of regionRows) {
      const row = exact(v, ['node', 'region', 'region_source', 'region_blocker', 'distance_class']);
      requireShape(hash(row.node) && ids.has(row.node) && !rows.has(row.node) && (row.node as string) > lastRowNode);
      requireShape((regionCodes as readonly unknown[]).includes(row.region) && (regionSourceKinds as readonly unknown[]).includes(row.region_source));
      requireShape(row.region_blocker === null || (regionBlockers as readonly unknown[]).includes(row.region_blocker));
      requireShape(row.distance_class === null || (distanceClasses as readonly unknown[]).includes(row.distance_class));
      rows.set(row.node, row); lastRowNode = row.node as string;
    }
    for (const v of r.nodes) {
      const n = v as Record<string, unknown>;
      const row = rows.get(n.id);
      requireShape(row !== undefined && row.region === n.region && row.region_source === n.region_source);
    }
  }
  // Server recomputes both canonical digests and exact source equivalence. The
  // browser checks the closed public shape; hashes are references, not signatures.
  return value as RouteExplorer;
}

const EVIDENCE_KEYS = ['protocol', 'subject_ref', 'subject_kind', 'source_kind', 'region', 'distance_class', 'vocabulary_version', 'model_version', 'uncertainty', 'issued_at_unix_ms', 'expires_at_unix_ms', 'authority_digest', 'digest'];
export function decodeCoarseRegionEvidence(value: unknown): readonly CoarseRegionEvidence[] {
  requireShape(Array.isArray(value) && value.length <= 4096);
  return (value as readonly unknown[]).map((item) => {
    const r = exact(item, EVIDENCE_KEYS);
    requireShape(r.protocol === 'mycelium.coarse_region_evidence.v1' && r.vocabulary_version === 'mycelium.coarse_region.v1');
    requireShape(hash(r.subject_ref) && hash(r.authority_digest) && hash(r.digest));
    requireShape(r.subject_kind === 'member' || r.subject_kind === 'relay');
    requireShape((evidenceSourceKinds as readonly unknown[]).includes(r.source_kind));
    requireShape((regionCodes as readonly unknown[]).includes(r.region));
    requireShape(r.uncertainty === evidenceUncertainty[r.source_kind as EvidenceSourceKind]);
    if (r.source_kind === 'latency_distance_class') {
      requireShape(r.region === 'unknown' && (distanceClasses as readonly unknown[]).includes(r.distance_class) && typeof r.model_version === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(r.model_version));
    } else {
      requireShape(r.distance_class === null && r.model_version === null);
    }
    for (const k of ['issued_at_unix_ms', 'expires_at_unix_ms']) requireShape(integer(r[k]));
    requireShape((r.expires_at_unix_ms as number) >= (r.issued_at_unix_ms as number));
    return r as unknown as CoarseRegionEvidence;
  });
}

export function effectiveFreshness(s: RouteSegment, now: number, connected: boolean): RouteSegment['freshness'] {
  if (s.issued_at !== null && now < s.issued_at) return 'unknown';
  if (!connected || (s.expires_at !== null && now > s.expires_at)) return 'stale';
  return s.freshness;
}
