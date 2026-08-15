export const ARTIFACT_ACQUISITION_PATH = '/__mycelium/artifacts/acquisitions';

const digest = /^sha256:[0-9a-f]{64}$/;
const revision = /^[0-9a-f]{40}$/;
const sourceReference = /^source-[0-9a-f]{12}$/;
const acquisitionStates = new Set(['pending', 'reserving', 'discovering_sources', 'transferring', 'verifying_chunks', 'verifying_pack', 'promoting', 'ready', 'cancelling', 'cancelled', 'failed']);
const terminalStates = new Set(['ready', 'cancelled', 'failed']);
const sourceStates = new Set(['eligible', 'active', 'rotated', 'lost', 'origin']);
const statusFields = ['protocol', 'generation', 'acquisition_id', 'state', 'phase', 'model_id', 'model_revision', 'representation', 'assignment_id', 'placement_id', 'stage_id', 'layer_start', 'layer_end_exclusive', 'total_bytes', 'cached_verified_bytes', 'transferred_verified_bytes', 'missing_bytes', 'quarantined_bytes', 'duplicate_bytes_prevented', 'eligible_source_count', 'active_source_count', 'sources', 'origin_bytes', 'aggregate_bytes_per_second', 'eta_seconds', 'chunk_count', 'verified_chunk_count', 'resumed_chunk_count', 'source_rotation_count', 'manifest_digest', 'assignment_digest', 'representation_digest', 'feasibility_digest', 'evidence_generation', 'promotion_digest', 'reason_code', 'retryable', 'started_at_unix_ms', 'updated_at_unix_ms', 'terminal_at_unix_ms'] as const;

export type ArtifactAcquisitionState = 'pending' | 'reserving' | 'discovering_sources' | 'transferring' | 'verifying_chunks' | 'verifying_pack' | 'promoting' | 'ready' | 'cancelling' | 'cancelled' | 'failed';
export type ArtifactSourceState = 'eligible' | 'active' | 'rotated' | 'lost' | 'origin';

export interface ArtifactSourceStatus {
  readonly source_ref: string;
  readonly state: ArtifactSourceState;
  readonly verified_bytes: number;
}

export interface ArtifactAcquisitionStatus {
  readonly protocol: 'mycelium.swarm_artifact_acquisition.v1';
  readonly generation: number;
  readonly acquisition_id: string;
  readonly state: ArtifactAcquisitionState;
  readonly phase: ArtifactAcquisitionState | null;
  readonly model_id: string;
  readonly model_revision: string;
  readonly representation: string;
  readonly assignment_id: string;
  readonly placement_id: string;
  readonly stage_id: string;
  readonly layer_start: number;
  readonly layer_end_exclusive: number;
  readonly total_bytes: number;
  readonly cached_verified_bytes: number;
  readonly transferred_verified_bytes: number;
  readonly missing_bytes: number;
  readonly quarantined_bytes: number;
  readonly duplicate_bytes_prevented: number;
  readonly eligible_source_count: number;
  readonly active_source_count: number;
  readonly sources: readonly ArtifactSourceStatus[];
  readonly origin_bytes: number;
  readonly aggregate_bytes_per_second: number;
  readonly eta_seconds: number | null;
  readonly chunk_count: number;
  readonly verified_chunk_count: number;
  readonly resumed_chunk_count: number;
  readonly source_rotation_count: number;
  readonly manifest_digest: string;
  readonly assignment_digest: string;
  readonly representation_digest: string;
  readonly feasibility_digest: string;
  readonly evidence_generation: number;
  readonly promotion_digest: string | null;
  readonly reason_code: string | null;
  readonly retryable: boolean;
  readonly started_at_unix_ms: number;
  readonly updated_at_unix_ms: number;
  readonly terminal_at_unix_ms: number | null;
}

export interface ArtifactAcquisitionLedger {
  readonly protocol: 'mycelium.swarm_artifact_acquisition_ledger.v1';
  readonly generation: number;
  readonly current: ArtifactAcquisitionStatus | null;
  readonly history: readonly ArtifactAcquisitionStatus[];
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new TypeError(`${label} is invalid`);
  return value as Record<string, unknown>;
}

function exact(value: Record<string, unknown>, fields: readonly string[], label: string): void {
  if (Object.keys(value).sort().join('\0') !== [...fields].sort().join('\0')) throw new TypeError(`${label} has unknown or missing fields`);
}

function text(value: unknown, label: string, maximum = 256): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > maximum) throw new TypeError(`${label} is invalid`);
  return value;
}

function integer(value: unknown, label: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || Number(value) < minimum) throw new TypeError(`${label} is invalid`);
  return Number(value);
}

function finite(value: unknown, label: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new TypeError(`${label} is invalid`);
  return value;
}

function optionalInteger(value: unknown, label: string): number | null {
  return value === null ? null : integer(value, label, 1);
}

function optionalDigest(value: unknown, label: string): string | null {
  if (value === null) return null;
  const result = text(value, label, 71);
  if (!digest.test(result)) throw new TypeError(`${label} is invalid`);
  return result;
}

export function decodeArtifactAcquisitionStatus(value: unknown): ArtifactAcquisitionStatus {
  const item = record(value, 'artifact acquisition');
  exact(item, statusFields, 'artifact acquisition');
  if (item.protocol !== 'mycelium.swarm_artifact_acquisition.v1') throw new TypeError('artifact acquisition protocol is invalid');
  const state = text(item.state, 'artifact acquisition state') as ArtifactAcquisitionState;
  if (!acquisitionStates.has(state)) throw new TypeError('artifact acquisition state is invalid');
  const phase = item.phase === null ? null : text(item.phase, 'artifact acquisition phase') as ArtifactAcquisitionState;
  if (phase !== null && !acquisitionStates.has(phase)) throw new TypeError('artifact acquisition phase is invalid');
  const modelRevision = text(item.model_revision, 'artifact model revision', 40);
  if (!revision.test(modelRevision)) throw new TypeError('artifact model revision is invalid');
  const sourcesRaw = item.sources;
  if (!Array.isArray(sourcesRaw) || sourcesRaw.length > 64) throw new TypeError('artifact sources are invalid');
  const sources = sourcesRaw.map((raw): ArtifactSourceStatus => {
    const source = record(raw, 'artifact source'); exact(source, ['source_ref', 'state', 'verified_bytes'], 'artifact source');
    const sourceRef = text(source.source_ref, 'artifact source reference', 19);
    const sourceState = text(source.state, 'artifact source state', 16) as ArtifactSourceState;
    if (!sourceReference.test(sourceRef) || !sourceStates.has(sourceState)) throw new TypeError('artifact source is invalid');
    return Object.freeze({ source_ref: sourceRef, state: sourceState, verified_bytes: integer(source.verified_bytes, 'artifact source bytes') });
  });
  if (sources.map((source) => source.source_ref).join('\0') !== [...sources].map((source) => source.source_ref).sort().join('\0') || new Set(sources.map((source) => source.source_ref)).size !== sources.length) throw new TypeError('artifact sources are unordered');
  const totalBytes = integer(item.total_bytes, 'artifact total bytes');
  const cachedBytes = integer(item.cached_verified_bytes, 'artifact cached bytes');
  const transferredBytes = integer(item.transferred_verified_bytes, 'artifact transferred bytes');
  const missingBytes = integer(item.missing_bytes, 'artifact missing bytes');
  const activeSources = integer(item.active_source_count, 'artifact active sources');
  const eligibleSources = integer(item.eligible_source_count, 'artifact eligible sources');
  const originBytes = integer(item.origin_bytes, 'artifact origin bytes');
  const sourceBytes = sources.reduce((total, source) => total + source.verified_bytes, 0);
  const chunkCount = integer(item.chunk_count, 'artifact chunk count');
  const verifiedChunks = integer(item.verified_chunk_count, 'artifact verified chunks');
  const resumedChunks = integer(item.resumed_chunk_count, 'artifact resumed chunks');
  const promotion = optionalDigest(item.promotion_digest, 'artifact promotion digest');
  const reason = item.reason_code === null ? null : text(item.reason_code, 'artifact reason code', 128);
  const started = integer(item.started_at_unix_ms, 'artifact started time', 1);
  const updated = integer(item.updated_at_unix_ms, 'artifact updated time', started);
  const terminal = optionalInteger(item.terminal_at_unix_ms, 'artifact terminal time');
  if (terminal !== null && terminal < updated) throw new TypeError('artifact terminal time is invalid');
  for (const field of ['manifest_digest', 'assignment_digest', 'representation_digest', 'feasibility_digest'] as const) if (!digest.test(text(item[field], `artifact ${field}`, 71))) throw new TypeError(`artifact ${field} is invalid`);
  if (typeof item.retryable !== 'boolean' || cachedBytes + transferredBytes + missingBytes !== totalBytes || activeSources > eligibleSources || activeSources > sources.length || sourceBytes + originBytes !== transferredBytes || verifiedChunks > chunkCount || resumedChunks > verifiedChunks || terminalStates.has(state) !== (terminal !== null) || (state === 'ready') !== (promotion !== null) || (state === 'failed') !== (reason !== null) || (terminalStates.has(state) && phase !== null)) throw new TypeError('artifact acquisition accounting is invalid');
  const layerStart = integer(item.layer_start, 'artifact layer start');
  const layerEnd = integer(item.layer_end_exclusive, 'artifact layer end', 1);
  if (layerEnd <= layerStart) throw new TypeError('artifact layer range is invalid');
  return Object.freeze({
    protocol: item.protocol, generation: integer(item.generation, 'artifact generation', 1), acquisition_id: text(item.acquisition_id, 'artifact acquisition id', 128), state, phase,
    model_id: text(item.model_id, 'artifact model id'), model_revision: modelRevision, representation: text(item.representation, 'artifact representation', 128), assignment_id: text(item.assignment_id, 'artifact assignment id', 128), placement_id: text(item.placement_id, 'artifact placement id', 128), stage_id: text(item.stage_id, 'artifact stage id', 128),
    layer_start: layerStart, layer_end_exclusive: layerEnd, total_bytes: totalBytes, cached_verified_bytes: cachedBytes, transferred_verified_bytes: transferredBytes, missing_bytes: missingBytes, quarantined_bytes: integer(item.quarantined_bytes, 'artifact quarantined bytes'), duplicate_bytes_prevented: integer(item.duplicate_bytes_prevented, 'artifact duplicate bytes'), eligible_source_count: eligibleSources, active_source_count: activeSources, sources: Object.freeze(sources), origin_bytes: originBytes, aggregate_bytes_per_second: finite(item.aggregate_bytes_per_second, 'artifact transfer rate'), eta_seconds: item.eta_seconds === null ? null : finite(item.eta_seconds, 'artifact ETA'), chunk_count: chunkCount, verified_chunk_count: verifiedChunks, resumed_chunk_count: resumedChunks, source_rotation_count: integer(item.source_rotation_count, 'artifact source rotations'), manifest_digest: item.manifest_digest as string, assignment_digest: item.assignment_digest as string, representation_digest: item.representation_digest as string, feasibility_digest: item.feasibility_digest as string, evidence_generation: integer(item.evidence_generation, 'artifact evidence generation'), promotion_digest: promotion, reason_code: reason, retryable: item.retryable, started_at_unix_ms: started, updated_at_unix_ms: updated, terminal_at_unix_ms: terminal,
  });
}

export function decodeArtifactAcquisitionLedger(value: unknown): ArtifactAcquisitionLedger {
  const item = record(value, 'artifact acquisition ledger');
  exact(item, ['protocol', 'generation', 'current', 'history'], 'artifact acquisition ledger');
  if (item.protocol !== 'mycelium.swarm_artifact_acquisition_ledger.v1' || !Array.isArray(item.history) || item.history.length > 256) throw new TypeError('artifact acquisition ledger is invalid');
  const current = item.current === null ? null : decodeArtifactAcquisitionStatus(item.current);
  const history = item.history.map(decodeArtifactAcquisitionStatus);
  const all = [...history, ...(current === null ? [] : [current])];
  const generation = integer(item.generation, 'artifact acquisition ledger generation');
  if ((current !== null && terminalStates.has(current.state)) || history.some((status) => !terminalStates.has(status.state)) || history.map((status) => status.generation).join('\0') !== [...history].map((status) => status.generation).sort((left, right) => left - right).join('\0') || new Set(history.map((status) => status.generation)).size !== history.length || new Set(all.map((status) => status.acquisition_id)).size !== all.length || generation !== Math.max(0, ...all.map((status) => status.generation))) throw new TypeError('artifact acquisition ledger is inconsistent');
  return Object.freeze({ protocol: item.protocol, generation, current, history: Object.freeze(history) });
}

export interface ArtifactAcquisitionClient { load(signal?: AbortSignal): Promise<ArtifactAcquisitionLedger>; }

export class HttpArtifactAcquisitionClient implements ArtifactAcquisitionClient {
  async load(signal?: AbortSignal): Promise<ArtifactAcquisitionLedger> {
    const response = await fetch(ARTIFACT_ACQUISITION_PATH, { method: 'GET', signal, cache: 'no-store', credentials: 'same-origin', redirect: 'error', referrerPolicy: 'no-referrer', headers: { accept: 'application/json' } });
    if (!response.ok) throw new Error(`artifact_acquisition_http_${response.status}`);
    if (!(response.headers.get('content-type')?.toLowerCase().startsWith('application/json') ?? false)) throw new Error('artifact_acquisition_content_type_invalid');
    return decodeArtifactAcquisitionLedger(await response.json());
  }
}
