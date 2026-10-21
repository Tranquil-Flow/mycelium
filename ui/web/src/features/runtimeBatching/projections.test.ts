// SPDX-License-Identifier: AGPL-3.0-only
import { describe, expect, it } from 'vitest';
import {
  RuntimeBatchingProjectionError,
  WORKSPACES,
  observedBatches,
  projectAllWorkspaces,
  projectNodes,
  projectReadiness,
  projectSettings,
  requestBatchSizes,
  sourceFromLiveStatus,
  type RuntimeBatchingSource,
  type RuntimePublication,
} from './projections';

const ref = (seed: string) => 'sha256:' + seed.repeat(64).slice(0, 64);

function interval(request: string, stage: number, batch: string, start: number) {
  return {
    interval_ref: `interval:${request}:${stage}:${start}`,
    hop_kind: 'compute' as const,
    request_ref: ref(request),
    microbatch_ref: ref(request + 'f'),
    stage_index: stage,
    phase: 'DECODE',
    started_at: start,
    finished_at: start + 0.1,
    runtime_batch_ref: batch,
  };
}

function publication(batched: boolean): RuntimePublication {
  return {
    protocol: 'mycelium.a10_live_publication.v1',
    stage_count: 2,
    intervals: [
      interval('a', 0, batched ? 'batch-1' : '', 0.0),
      interval('b', 0, batched ? 'batch-1' : '', 0.0),
      interval('c', 0, '', 0.5),
    ],
    dropped_interval_count: 0,
    runtime_batch_observations: [
      {
        batch_ref: 'batch-1',
        phase: 'DECODE',
        stage: 'stage-000',
        backend_invocations: batched ? 1 : 2,
        membership_authority: 'stage_runtime',
        proposed_member_count: 2,
        actual_member_count: 2,
        member_outcomes: [
          [ref('1'), 'completed'],
          [ref('2'), batched ? 'completed' : 'cancelled'],
        ],
        claims_runtime_batch: batched,
      },
    ],
    dropped_observation_count: 0,
  };
}

function source(overrides: Partial<RuntimeBatchingSource> = {}): RuntimeBatchingSource {
  return {
    freshness: 'live',
    peers: [
      { nodeLabel: 'Node 1', publication: publication(true) },
      { nodeLabel: 'Node 2', publication: null },
    ],
    overlap: { observed: true, reason: 'observed_stage_compute_intersection', intersectionSeconds: 0.012 },
    verifier: null,
    throughputDecision: null,
    ...overrides,
  };
}

describe('eight-workspace runtime batching projection', () => {
  it('projects every workspace from one generation', () => {
    const projections = projectAllWorkspaces(source());
    expect(projections.map((item) => item.workspace)).toEqual([...WORKSPACES]);
    expect(new Set(projections.map((item) => item.freshness))).toEqual(new Set(['live']));
  });

  it('reports only runtime-owned batches of one invocation and two or more members', () => {
    expect(observedBatches(source())).toHaveLength(1);
    const split = source({ peers: [{ nodeLabel: 'Node 1', publication: publication(false) }] });
    expect(observedBatches(split)).toHaveLength(0);
    expect(requestBatchSizes(source()).get(ref('a'))).toBe(2);
    expect(requestBatchSizes(source()).has(ref('c'))).toBe(false);
  });

  it('renders missing evidence as unknown, never zero or success', () => {
    const empty = source({ peers: [{ nodeLabel: 'Node 1', publication: null }], overlap: null });
    const readiness = projectReadiness(empty);
    const backend = readiness.facts.find((item) => item.label === 'Backend batch');
    expect(backend?.value).toBe('unknown');
    expect(backend?.state).toBe('unknown');
    const overlap = readiness.facts.find((item) => item.label === 'Pipeline overlap');
    expect(overlap?.value).toBe('unknown');
    const nodes = projectNodes(empty);
    expect(nodes.facts.every((item) => item.value !== '0')).toBe(true);
  });

  it('keeps settings disabled until a live qualified verifier and material benchmark exist', () => {
    const blocked = projectSettings(
      source({
        verifier: {
          decision: 'not_qualified',
          matrixPassed: true,
          blockers: ['direct_runtime_call_cannot_qualify'],
          widthLimits: { one: 1, qualified_default: 4, qualified_maximum: 8 },
          provenance: ['live'],
        },
      }),
    );
    expect(blocked.facts.map((item) => item.state)).toEqual(['disabled', 'disabled']);
    expect(blocked.facts[0].reason).toBe('direct_runtime_call_cannot_qualify');

    const qualified = projectSettings(
      source({
        verifier: {
          decision: 'qualified',
          matrixPassed: true,
          blockers: [],
          widthLimits: { one: 1, qualified_default: 4, qualified_maximum: 8 },
          provenance: ['live'],
        },
        throughputDecision: 'material',
      }),
    );
    expect(qualified.facts[0]).toMatchObject({ value: '8', state: 'observed' });
    expect(qualified.facts[1].state).toBe('observed');

    const stale = projectSettings(
      source({
        freshness: 'stale',
        verifier: {
          decision: 'qualified',
          matrixPassed: true,
          blockers: [],
          widthLimits: { one: 1, qualified_default: 4, qualified_maximum: 8 },
          provenance: ['live'],
        },
        throughputDecision: 'material',
      }),
    );
    expect(stale.facts.map((item) => item.state)).toEqual(['disabled', 'disabled']);
  });

  it('marks fixture verifier evidence as non-qualifying in the device lab', () => {
    const [, deviceLab] = projectAllWorkspaces(
      source({
        verifier: {
          decision: 'not_qualified',
          matrixPassed: true,
          blockers: ['non_live_provenance_cannot_qualify'],
          widthLimits: { one: 1, qualified_default: 3, qualified_maximum: 6 },
          provenance: ['fixture'],
        },
      }),
    );
    expect(deviceLab.facts.find((item) => item.label === 'Evidence source')?.state).toBe('disabled');
  });

  it('records member-scoped incidents from runtime outcomes', () => {
    const incidents = projectAllWorkspaces(
      source({ peers: [{ nodeLabel: 'Node 1', publication: publication(false) }] }),
    ).find((item) => item.workspace === 'incidents');
    expect(incidents?.facts.some((item) => item.label === 'Batch member cancelled')).toBe(true);
  });

  it('rejects raw request identities instead of rendering them', () => {
    const leaky = publication(true);
    leaky.intervals[0].request_ref = 'request-alice-1';
    expect(() => projectAllWorkspaces(source({ peers: [{ nodeLabel: 'Node 1', publication: leaky }] }))).toThrow(
      RuntimeBatchingProjectionError,
    );
  });

  it('never uses internal gate labels in product copy', () => {
    const text = JSON.stringify(projectAllWorkspaces(source()));
    expect(text).not.toMatch(/\bA1[0-9]\b|\bM[0-9]{1,2}\b/);
  });

  it('reads live status without inventing publications', () => {
    const projected = sourceFromLiveStatus(
      { peers: [{ node_id: 'node-0', a10_publication: publication(true) }, { node_id: 'node-2' }] },
      'live',
    );
    expect(projected.peers.map((peer) => peer.publication === null)).toEqual([false, true]);
    expect(sourceFromLiveStatus(null, 'live').freshness).toBe('unknown');
  });
});
