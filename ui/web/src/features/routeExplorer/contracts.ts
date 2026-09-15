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
export interface RouteNode { readonly id: string; readonly kind: 'member' | 'endpoint'; readonly region: 'unknown'; readonly region_source: 'unavailable' }
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
  const r = exact(value, ['protocol', 'generation', 'source_mode', 'source_digest', 'digest', 'overlays', 'nodes', 'segments']);
  requireShape(r.protocol === 'mycelium.route_explorer.v1' && r.generation === generation && r.source_mode === mode);
  requireShape(hash(r.source_digest) && hash(r.digest) && r.overlays === 'unavailable');
  requireShape(Array.isArray(r.nodes) && r.nodes.length <= 4096 && Array.isArray(r.segments) && r.segments.length <= 4096);
  const ids = new Set<unknown>();
  let lastNode = '';
  for (const v of r.nodes) {
    const n = exact(v, ['id', 'kind', 'region', 'region_source']);
    requireShape(hash(n.id) && !ids.has(n.id) && (n.id as string) > lastNode);
    requireShape(['member', 'endpoint'].includes(n.kind as string) && n.region === 'unknown' && n.region_source === 'unavailable');
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
  // Server recomputes both canonical digests and exact source equivalence. The
  // browser checks the closed public shape; hashes are references, not signatures.
  return value as RouteExplorer;
}

export function effectiveFreshness(s: RouteSegment, now: number, connected: boolean): RouteSegment['freshness'] {
  if (s.issued_at !== null && now < s.issued_at) return 'unknown';
  if (!connected || (s.expires_at !== null && now > s.expires_at)) return 'stale';
  return s.freshness;
}
