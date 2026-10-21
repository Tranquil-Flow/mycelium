// SPDX-License-Identifier: AGPL-3.0-only
//
// Eight-workspace projection of runtime batching, pipeline overlap and target
// verification.
//
// The source is the route's live status: each node's co-signed runtime publication
// (stage-compute intervals and runtime-owned batch observations), plus the owner's
// verifier and batch qualification summaries when they exist.  Every helper here is
// pure and side-effect free.
//
// Invariants:
//
//   * the runtime is the only batch authority -- the UI never authors membership,
//     never derives a batch from adjacency, and reports only what an observation
//     with one backend invocation and two or more members says;
//   * unknown-not-zero -- a missing publication, verdict or qualification renders
//     as "unknown" (or a disabled reason), never as 0 or success;
//   * privacy -- request identities must be sha256 pseudonyms; anything else is
//     rejected at the boundary rather than rendered;
//   * product copy uses human capability names, never internal gate labels.

export type Freshness = 'live' | 'stale' | 'historical' | 'fixture' | 'unknown';

export interface PublishedInterval {
  interval_ref: string;
  hop_kind: 'compute' | 'commit';
  request_ref: string;
  microbatch_ref: string;
  stage_index: number;
  phase: string;
  started_at: number;
  finished_at: number;
  runtime_batch_ref: string;
}

export interface PublishedBatchObservation {
  batch_ref: string;
  phase: string;
  stage: string;
  backend_invocations: number;
  membership_authority: string;
  proposed_member_count: number;
  actual_member_count: number;
  member_outcomes: [string, string][];
  claims_runtime_batch: boolean;
}

export interface RuntimePublication {
  protocol: 'mycelium.a10_live_publication.v1';
  stage_count: number;
  intervals: PublishedInterval[];
  dropped_interval_count: number;
  runtime_batch_observations: PublishedBatchObservation[];
  dropped_observation_count: number;
}

export interface PeerPublication {
  nodeLabel: string;
  publication: RuntimePublication | null;
}

export interface VerifierQualificationSummary {
  decision: 'qualified' | 'not_qualified';
  matrixPassed: boolean;
  blockers: string[];
  widthLimits: { one: number | null; qualified_default: number | null; qualified_maximum: number | null };
  provenance: string[];
}

export interface OverlapVerdictSummary {
  observed: boolean;
  reason: string;
  intersectionSeconds: number | null;
}

export interface RuntimeBatchingSource {
  freshness: Freshness;
  peers: PeerPublication[];
  overlap: OverlapVerdictSummary | null;
  verifier: VerifierQualificationSummary | null;
  throughputDecision: 'material' | 'not_material' | 'inconclusive' | null;
}

export class RuntimeBatchingProjectionError extends Error {
  constructor(code: string) {
    super(code);
    this.name = 'RuntimeBatchingProjectionError';
  }
}

const PSEUDONYM_RE = /^sha256:[0-9a-f]{64}$/;
const UNKNOWN = 'unknown';

export const WORKSPACES = [
  'inference',
  'device_lab',
  'network',
  'nodes',
  'plans',
  'readiness',
  'incidents',
  'settings',
] as const;
export type Workspace = (typeof WORKSPACES)[number];

/** A projected fact: a value or an explicit unknown/disabled state with a reason. */
export interface Fact {
  label: string;
  value: string;
  state: 'observed' | 'unknown' | 'disabled' | 'failed';
  reason?: string;
}

export interface WorkspaceProjection {
  workspace: Workspace;
  freshness: Freshness;
  facts: Fact[];
}

function fact(label: string, value: string | number | null, reason?: string): Fact {
  if (value === null) return { label, value: UNKNOWN, state: 'unknown', reason };
  return { label, value: String(value), state: 'observed' };
}

function disabled(label: string, reason: string): Fact {
  return { label, value: 'disabled', state: 'disabled', reason };
}

/** Reject anything that is not a pseudonymous request identity. */
export function validatePublication(publication: RuntimePublication): RuntimePublication {
  if (publication.protocol !== 'mycelium.a10_live_publication.v1') {
    throw new RuntimeBatchingProjectionError('publication_protocol_invalid');
  }
  for (const interval of publication.intervals) {
    if (!PSEUDONYM_RE.test(interval.request_ref) || !PSEUDONYM_RE.test(interval.microbatch_ref)) {
      throw new RuntimeBatchingProjectionError('publication_identity_not_pseudonymous');
    }
    if (!(interval.finished_at >= interval.started_at)) {
      throw new RuntimeBatchingProjectionError('publication_interval_reversed');
    }
  }
  return publication;
}

function publications(source: RuntimeBatchingSource): RuntimePublication[] {
  return source.peers
    .map((peer) => peer.publication)
    .filter((item): item is RuntimePublication => item !== null)
    .map(validatePublication);
}

/** Observations the runtime itself says were one backend invocation of 2+ members. */
export function observedBatches(source: RuntimeBatchingSource): PublishedBatchObservation[] {
  return publications(source).flatMap((publication) =>
    publication.runtime_batch_observations.filter(
      (observation) =>
        observation.claims_runtime_batch &&
        observation.backend_invocations === 1 &&
        observation.membership_authority === 'stage_runtime' &&
        observation.actual_member_count >= 2,
    ),
  );
}

/** Batch size per pseudonymous request, from runtime-owned batch references only. */
export function requestBatchSizes(source: RuntimeBatchingSource): Map<string, number> {
  const members = new Map<string, Set<string>>();
  for (const publication of publications(source)) {
    for (const interval of publication.intervals) {
      if (interval.hop_kind !== 'compute' || !interval.runtime_batch_ref) continue;
      const key = `${interval.stage_index}:${interval.runtime_batch_ref}`;
      const set = members.get(key) ?? new Set<string>();
      set.add(interval.request_ref);
      members.set(key, set);
    }
  }
  const sizes = new Map<string, number>();
  for (const set of members.values()) {
    for (const request of set) {
      sizes.set(request, Math.max(sizes.get(request) ?? 0, set.size));
    }
  }
  return sizes;
}

function batchMode(publication: RuntimePublication | null): string | null {
  if (publication === null) return null;
  if (publication.runtime_batch_observations.some((item) => item.claims_runtime_batch)) {
    return 'runtime batch';
  }
  return publication.runtime_batch_observations.length > 0 ? 'one member at a time' : 'no batch observed';
}

function overlapFact(source: RuntimeBatchingSource): Fact {
  if (source.overlap === null) return fact('Pipeline overlap', null, 'no causal trace verdict');
  if (source.overlap.observed) {
    return fact(
      'Pipeline overlap',
      `observed (${source.overlap.intersectionSeconds === null ? UNKNOWN : `${(source.overlap.intersectionSeconds * 1000).toFixed(1)} ms`} after clock uncertainty)`,
    );
  }
  return { label: 'Pipeline overlap', value: 'not observed', state: 'observed', reason: source.overlap.reason };
}

function verifierFact(source: RuntimeBatchingSource): Fact {
  const verifier = source.verifier;
  if (verifier === null) return fact('Target verification', null, 'no executed verifier matrix');
  if (verifier.decision === 'qualified') return fact('Target verification', 'qualified');
  return {
    label: 'Target verification',
    value: verifier.matrixPassed ? 'matrix passed, not qualified' : 'not qualified',
    state: verifier.matrixPassed ? 'observed' : 'failed',
    reason: verifier.blockers.join(', '),
  };
}

function isQualifiedLive(source: RuntimeBatchingSource): boolean {
  return (
    source.freshness === 'live' &&
    source.verifier?.decision === 'qualified' &&
    !source.verifier.provenance.some((item) => item !== 'live')
  );
}

export function projectInference(source: RuntimeBatchingSource): WorkspaceProjection {
  const sizes = requestBatchSizes(source);
  const facts: Fact[] = [];
  if (sizes.size === 0) {
    facts.push(fact('Batched requests', null, 'no runtime batch reference observed'));
  }
  for (const [request, size] of [...sizes.entries()].sort()) {
    facts.push(fact(`Request ${request.slice(7, 15)}`, `batch of ${size}`));
  }
  facts.push(verifierFact(source));
  return { workspace: 'inference', freshness: source.freshness, facts };
}

export function projectDeviceLab(source: RuntimeBatchingSource): WorkspaceProjection {
  const verifier = source.verifier;
  const facts: Fact[] = [verifierFact(source)];
  if (verifier !== null) {
    facts.push(fact('Qualified default width', verifier.widthLimits.qualified_default));
    facts.push(fact('Qualified maximum width', verifier.widthLimits.qualified_maximum));
    if (verifier.provenance.some((item) => item !== 'live')) {
      facts.push({ label: 'Evidence source', value: verifier.provenance.join(', '), state: 'disabled', reason: 'non-live evidence cannot qualify' });
    }
  }
  facts.push(fact('Runtime batches observed', publications(source).length === 0 ? null : observedBatches(source).length));
  return { workspace: 'device_lab', freshness: source.freshness, facts };
}

export function projectNetwork(source: RuntimeBatchingSource): WorkspaceProjection {
  const facts: Fact[] = [overlapFact(source)];
  const pubs = publications(source);
  if (pubs.length === 0) {
    facts.push(fact('Stage compute intervals', null, 'no runtime publication'));
  }
  for (const [index, publication] of pubs.entries()) {
    const compute = publication.intervals.filter((item) => item.hop_kind === 'compute');
    const batched = compute.filter((item) => item.runtime_batch_ref).length;
    facts.push(fact(`Publication ${index + 1} stage compute`, `${compute.length} intervals, ${batched} in a runtime batch`));
  }
  return { workspace: 'network', freshness: source.freshness, facts };
}

export function projectNodes(source: RuntimeBatchingSource): WorkspaceProjection {
  const facts = source.peers.flatMap((peer) => {
    const publication = peer.publication === null ? null : validatePublication(peer.publication);
    const largest =
      publication === null || publication.runtime_batch_observations.length === 0
        ? null
        : Math.max(...publication.runtime_batch_observations.map((item) => item.actual_member_count));
    return [
      fact(`${peer.nodeLabel} batch mode`, batchMode(publication), publication === null ? 'no runtime publication' : undefined),
      fact(`${peer.nodeLabel} largest actual batch`, largest),
      fact(`${peer.nodeLabel} dropped history`, publication === null ? null : publication.dropped_interval_count),
    ];
  });
  return { workspace: 'nodes', freshness: source.freshness, facts };
}

export function projectPlans(source: RuntimeBatchingSource): WorkspaceProjection {
  const observed = observedBatches(source);
  return {
    workspace: 'plans',
    freshness: source.freshness,
    facts: [
      fact('Modeled batch choice', null, 'planner intent is not an observation'),
      fact('Observed batch membership', publications(source).length === 0 ? null : observed.length),
      fact('Observed throughput gain', source.throughputDecision, 'no paired benchmark decision'),
    ],
  };
}

export function projectReadiness(source: RuntimeBatchingSource): WorkspaceProjection {
  const hasPublications = publications(source).length > 0;
  const facts: Fact[] = [
    fact('Backend batch', hasPublications ? (observedBatches(source).length > 0 ? 'observed' : 'not observed') : null),
    fact(
      'Continuous arrivals',
      hasPublications ? ([...requestBatchSizes(source).values()].some((size) => size >= 2) ? 'observed' : 'not observed') : null,
    ),
    overlapFact(source),
    verifierFact(source),
    fact('Throughput', source.throughputDecision, 'no paired benchmark decision'),
    fact('Freshness', source.freshness),
  ];
  return { workspace: 'readiness', freshness: source.freshness, facts };
}

export function projectIncidents(source: RuntimeBatchingSource): WorkspaceProjection {
  const facts: Fact[] = [];
  for (const observation of observedBatches(source).concat(
    publications(source).flatMap((item) => item.runtime_batch_observations.filter((obs) => !obs.claims_runtime_batch)),
  )) {
    for (const [, outcome] of observation.member_outcomes) {
      if (outcome === 'failed' || outcome === 'cancelled') {
        facts.push({ label: `Batch member ${outcome}`, value: 'member-scoped', state: 'failed', reason: observation.phase });
      }
    }
  }
  for (const blocker of source.verifier?.blockers ?? []) {
    facts.push({ label: 'Verifier blocker', value: blocker, state: 'failed' });
  }
  if (facts.length === 0) facts.push(fact('Incidents', publications(source).length === 0 ? null : 'none recorded'));
  return { workspace: 'incidents', freshness: source.freshness, facts };
}

export function projectSettings(source: RuntimeBatchingSource): WorkspaceProjection {
  const qualified = isQualifiedLive(source);
  const reason = source.verifier === null ? 'no executed verifier matrix' : source.verifier.blockers.join(', ') || 'evidence is not live';
  const facts: Fact[] = [
    qualified
      ? fact('Verification width limit', source.verifier?.widthLimits.qualified_maximum ?? null)
      : disabled('Verification width limit', reason),
    source.throughputDecision === 'material' && source.freshness === 'live'
      ? fact('Runtime batch policy', 'qualified future-request policy')
      : disabled('Runtime batch policy', 'no material same-route benchmark'),
  ];
  return { workspace: 'settings', freshness: source.freshness, facts };
}

const PROJECTORS: Record<Workspace, (source: RuntimeBatchingSource) => WorkspaceProjection> = {
  inference: projectInference,
  device_lab: projectDeviceLab,
  network: projectNetwork,
  nodes: projectNodes,
  plans: projectPlans,
  readiness: projectReadiness,
  incidents: projectIncidents,
  settings: projectSettings,
};

/** One current generation projected into all eight workspaces. */
export function projectAllWorkspaces(source: RuntimeBatchingSource): WorkspaceProjection[] {
  return WORKSPACES.map((workspace) => PROJECTORS[workspace](source));
}

/** Read the live-status peers into a projection source; never invents a publication. */
export function sourceFromLiveStatus(
  status: { peers?: { node_id?: unknown; a10_publication?: unknown }[] } | null,
  freshness: Freshness,
  extras: Partial<Pick<RuntimeBatchingSource, 'overlap' | 'verifier' | 'throughputDecision'>> = {},
): RuntimeBatchingSource {
  const peers = (status?.peers ?? []).map((peer, index) => ({
    nodeLabel: `Node ${index + 1}`,
    publication:
      peer.a10_publication && typeof peer.a10_publication === 'object'
        ? validatePublication(peer.a10_publication as RuntimePublication)
        : null,
  }));
  return {
    freshness: status === null ? 'unknown' : freshness,
    peers,
    overlap: extras.overlap ?? null,
    verifier: extras.verifier ?? null,
    throughputDecision: extras.throughputDecision ?? null,
  };
}
