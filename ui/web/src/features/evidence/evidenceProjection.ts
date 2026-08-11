export const RUNTIME_EVIDENCE_PATH = '/__mycelium/evidence/runtime';
export const HISTORICAL_EVIDENCE_PATH = '/__mycelium/evidence/history';

export type EvidenceSourceKind = 'live_runtime' | 'planner_intent' | 'sealed_historical' | 'replay' | 'fixture';
export type EvidenceFreshness = 'current' | 'degraded' | 'stale' | 'historical' | 'replay' | 'fixture';
export type EvidenceCapability = 'route_execution' | 'replicated_serving' | 'scoped_recovery' | 'speculative_decoding' | 'heterogeneous_participation' | 'release_closure' | 'stage_local_kv';

export type EvidenceProjection = Readonly<{
  protocol: 'mycelium.evidence_projection.v1';
  record_id: string;
  capability: EvidenceCapability;
  source_kind: EvidenceSourceKind;
  authority: string;
  generation: number;
  captured_at_unix_ms: number;
  observed_at_unix_ms: number;
  valid_until_unix_ms: number | null;
  freshness: EvidenceFreshness;
  payload_protocol: string;
  payload: Readonly<Record<string, unknown>>;
}>;

export type EvidenceHistory = Readonly<{
  protocol: 'mycelium.evidence_history.v1';
  records: readonly EvidenceProjection[];
}>;

const fields = ['protocol','record_id','capability','source_kind','authority','generation','captured_at_unix_ms','observed_at_unix_ms','valid_until_unix_ms','freshness','payload_protocol','payload'] as const;
const capabilities = new Set<EvidenceCapability>(['route_execution','replicated_serving','scoped_recovery','speculative_decoding','heterogeneous_participation','release_closure','stage_local_kv']);
const freshnessByKind: Readonly<Record<EvidenceSourceKind, ReadonlySet<EvidenceFreshness>>> = {
  live_runtime: new Set(['current','degraded','stale']),
  planner_intent: new Set(['current','degraded','stale']),
  sealed_historical: new Set(['historical']),
  replay: new Set(['replay']),
  fixture: new Set(['fixture']),
};

function exact(value: unknown, expected: readonly string[], path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new TypeError(`${path} must be an object`);
  const record = value as Record<string, unknown>;
  if (Object.keys(record).sort().join('|') !== [...expected].sort().join('|')) throw new TypeError(`${path} has unknown or missing fields`);
  return record;
}
function text(value: unknown, path: string): string { if (typeof value !== 'string' || value.length === 0 || value.length > 256) throw new TypeError(`${path} must be bounded text`); return value; }
function integer(value: unknown, path: string, positive = false): number { if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < (positive ? 1 : 0)) throw new TypeError(`${path} must be ${positive ? 'positive' : 'non-negative'} integer`); return value; }

export function decodeEvidenceProjection(value: unknown): EvidenceProjection {
  const source = exact(value, fields, 'evidence projection');
  if (source.protocol !== 'mycelium.evidence_projection.v1') throw new TypeError('evidence projection protocol is invalid');
  const capability = text(source.capability, 'capability') as EvidenceCapability;
  if (!capabilities.has(capability)) throw new TypeError('evidence capability is invalid');
  const sourceKind = text(source.source_kind, 'source kind') as EvidenceSourceKind;
  if (!(sourceKind in freshnessByKind)) throw new TypeError('evidence source kind is invalid');
  const freshness = text(source.freshness, 'freshness') as EvidenceFreshness;
  if (!freshnessByKind[sourceKind].has(freshness)) throw new TypeError('evidence source/freshness mismatch');
  const captured = integer(source.captured_at_unix_ms, 'captured at', true);
  const observed = integer(source.observed_at_unix_ms, 'observed at', true);
  if (captured < observed) throw new TypeError('evidence capture precedes observation');
  let validUntil: number | null = null;
  if (source.valid_until_unix_ms !== null) validUntil = integer(source.valid_until_unix_ms, 'valid until', true);
  const immutable = sourceKind === 'sealed_historical' || sourceKind === 'replay' || sourceKind === 'fixture';
  if ((immutable && validUntil !== null) || (!immutable && (validUntil === null || validUntil < captured))) throw new TypeError('evidence validity is invalid');
  const payloadProtocol = text(source.payload_protocol, 'payload protocol');
  if (typeof source.payload !== 'object' || source.payload === null || Array.isArray(source.payload) || (source.payload as Record<string, unknown>).protocol !== payloadProtocol) throw new TypeError('evidence payload protocol mismatch');
  return Object.freeze({
    protocol: 'mycelium.evidence_projection.v1',
    record_id: text(source.record_id, 'record id'),
    capability,
    source_kind: sourceKind,
    authority: text(source.authority, 'authority'),
    generation: integer(source.generation, 'generation'),
    captured_at_unix_ms: captured,
    observed_at_unix_ms: observed,
    valid_until_unix_ms: validUntil,
    freshness,
    payload_protocol: payloadProtocol,
    payload: Object.freeze({ ...(source.payload as Record<string, unknown>) }),
  });
}
export function decodeEvidenceHistory(value: unknown): EvidenceHistory {
  const source = exact(value, ['protocol','records'], 'evidence history');
  if (source.protocol !== 'mycelium.evidence_history.v1' || !Array.isArray(source.records) || source.records.length > 128) throw new TypeError('evidence history is invalid');
  const records = source.records.map(decodeEvidenceProjection);
  if (records.some((record) => record.source_kind !== 'sealed_historical' || record.freshness !== 'historical')) throw new TypeError('evidence history contains a non-historical record');
  return Object.freeze({ protocol: 'mycelium.evidence_history.v1', records: Object.freeze(records) });
}

export function evidenceIsCurrentLive(evidence: EvidenceProjection, nowUnixMs: number): boolean {
  return evidence.source_kind === 'live_runtime'
    && evidence.freshness === 'current'
    && evidence.valid_until_unix_ms !== null
    && evidence.valid_until_unix_ms >= nowUnixMs;
}

export interface EvidenceProjectionClient {
  loadRuntime(signal?: AbortSignal): Promise<EvidenceProjection>;
  loadHistory(signal?: AbortSignal): Promise<EvidenceHistory>;
}

export class HttpEvidenceProjectionClient implements EvidenceProjectionClient {
  constructor(private readonly fetcher: typeof fetch = globalThis.fetch.bind(globalThis)) {}
  private async request(path: string, signal?: AbortSignal): Promise<unknown> {
    const response = await this.fetcher(path, { method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { Accept: 'application/json' }, signal });
    if (!response.ok) throw new Error(`evidence_source_${response.status}`);
    return response.json();
  }
  async loadRuntime(signal?: AbortSignal): Promise<EvidenceProjection> { return decodeEvidenceProjection(await this.request(RUNTIME_EVIDENCE_PATH, signal)); }
  async loadHistory(signal?: AbortSignal): Promise<EvidenceHistory> { return decodeEvidenceHistory(await this.request(HISTORICAL_EVIDENCE_PATH, signal)); }
}
