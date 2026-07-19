import { describe, expect, it } from 'vitest';
import {
  PRODUCT_INFERENCE_PROTOCOL,
  PRODUCT_QUALIFIER_AUTHORITY,
  type ProductQualification,
} from './contracts';
import {
  createInitialProductState,
  normalizeOptionalMetric,
  productStateReducer,
  routeReadinessFromQualification,
} from './ProductState';

const digest = `sha256:${'c'.repeat(64)}`;

function qualification(
  routeReady: boolean,
  issuedAtUnixMs = 1_800_000_000_000,
): ProductQualification {
  return {
    protocol: PRODUCT_INFERENCE_PROTOCOL,
    issued_at_unix_ms: issuedAtUnixMs,
    evidence_class: routeReady ? 'physical_qualification' : 'synthetic_test_fixture',
    route_ready: routeReady,
    reason_codes: routeReady ? [] : ['physical_qualification_missing'],
    binding: {
      qualification_id: 'qualification-1',
      qualification_digest: digest,
      deployment_id: 'deployment-1',
      deployment_epoch: 1,
      topology_version: 1,
      model_id: 'model-1',
      resolved_commit: 'commit-1',
      manifest_digest: digest,
      path_manifest_digest: digest,
      stage_load_proof_digests: [digest],
    },
  };
}

describe('ProductState', () => {
  it('starts at inference with fixture truth and qualifier-only unknown readiness', () => {
    const state = createInitialProductState({ now_unix_ms: 1_800_000_000_001 });
    expect(state.active_route).toBe('inference');
    expect(state.source_mode).toBe('fixture');
    expect(state.route_readiness).toEqual({
      authority: PRODUCT_QUALIFIER_AUTHORITY,
      value: false,
      status: 'unknown',
      reasons: ['fixture_source_not_authoritative'],
    });
    expect(Object.isFrozen(state)).toBe(true);
    expect(Object.isFrozen(state.route_readiness.reasons)).toBe(true);
  });

  it('never promotes readiness from observatory or browser swarm metrics', () => {
    let state = createInitialProductState({ now_unix_ms: 1_800_000_000_001 });
    state = productStateReducer(state, {
      type: 'observatory_updated',
      generation: 42,
      source_mode: 'live',
    });
    state = productStateReducer(state, {
      type: 'browser_swarm_updated',
      ready_workers: 8,
    });
    expect(state.observatory_generation).toBe(42);
    expect(state.browser_ready_workers).toBe(8);
    expect(state.route_readiness.value).toBe(false);
    expect(state.route_readiness.authority).toBe(PRODUCT_QUALIFIER_AUTHORITY);
  });

  it('accepts only current physical qualifier evidence in live mode', () => {
    let state = createInitialProductState({
      source_mode: 'live',
      now_unix_ms: 1_800_000_000_001,
    });
    state = productStateReducer(state, {
      type: 'qualification_updated',
      qualification: qualification(true),
      now_unix_ms: 1_800_000_000_001,
    });
    expect(state.route_readiness).toMatchObject({ value: true, status: 'accepted' });

    state = productStateReducer(state, {
      type: 'clock_ticked',
      now_unix_ms: 1_800_300_000_001,
    });
    expect(state.route_readiness).toMatchObject({ value: false, status: 'blocked' });
    expect(state.route_readiness.reasons).toContain('Qualification is stale');
  });

  it('never rolls back time or replaces a newer qualification with delayed evidence', () => {
    let state = createInitialProductState({
      source_mode: 'live',
      now_unix_ms: 1_800_000_000_001,
    });
    state = productStateReducer(state, {
      type: 'qualification_updated',
      qualification: qualification(true),
      now_unix_ms: 1_800_000_000_001,
    });
    state = productStateReducer(state, {
      type: 'clock_ticked',
      now_unix_ms: 1_800_300_000_001,
    });
    state = productStateReducer(state, {
      type: 'clock_ticked',
      now_unix_ms: 1_800_000_000_002,
    });
    expect(state.now_unix_ms).toBe(1_800_300_000_001);
    expect(state.route_readiness).toMatchObject({ value: false, status: 'blocked' });

    const delayed = {
      ...qualification(false),
      issued_at_unix_ms: 1_799_999_999_999,
    };
    state = productStateReducer(state, {
      type: 'qualification_updated',
      qualification: delayed,
      now_unix_ms: 1_800_000_000_002,
    });
    expect(state.qualification?.issued_at_unix_ms).toBe(1_800_000_000_000);
    expect(state.now_unix_ms).toBe(1_800_300_000_001);
  });

  it('fails closed on conflicting evidence at the qualification watermark', () => {
    let state = createInitialProductState({
      source_mode: 'live',
      now_unix_ms: 1_800_000_000_001,
    });
    state = productStateReducer(state, {
      type: 'qualification_updated',
      qualification: qualification(true),
      now_unix_ms: 1_800_000_000_001,
    });
    const conflicting = {
      ...qualification(true),
      binding: {
        ...qualification(true).binding,
        qualification_digest: `sha256:${'d'.repeat(64)}`,
      },
    };
    state = productStateReducer(state, {
      type: 'qualification_updated',
      qualification: conflicting,
      now_unix_ms: 1_800_000_000_002,
    });
    expect(state.qualification).toBeNull();
    expect(state.route_readiness).toEqual({
      authority: PRODUCT_QUALIFIER_AUTHORITY,
      value: false,
      status: 'blocked',
      reasons: ['qualification_conflict_at_watermark'],
    });

    state = productStateReducer(state, {
      type: 'qualification_updated',
      qualification: qualification(true),
      now_unix_ms: 1_800_000_000_003,
    });
    state = productStateReducer(state, {
      type: 'clock_ticked',
      now_unix_ms: 1_800_000_000_004,
    });
    expect(state.qualification).toBeNull();
    expect(state.route_readiness.reasons).toEqual(['qualification_conflict_at_watermark']);

    state = productStateReducer(state, {
      type: 'qualification_updated',
      qualification: qualification(true, 1_800_000_000_010),
      now_unix_ms: 1_800_000_000_011,
    });
    expect(state.qualification_conflicted_at_watermark).toBe(false);
    expect(state.route_readiness.status).toBe('accepted');
  });

  it('clears accepted qualification when source changes to fixture or replay', () => {
    let state = createInitialProductState({
      source_mode: 'live',
      now_unix_ms: 1_800_000_000_001,
    });
    state = productStateReducer(state, {
      type: 'qualification_updated',
      qualification: qualification(true),
      now_unix_ms: 1_800_000_000_001,
    });
    state = productStateReducer(state, {
      type: 'observatory_updated',
      generation: 5,
      source_mode: 'replay',
    });
    expect(state.qualification).toBeNull();
    expect(state.route_readiness).toMatchObject({ value: false, status: 'unknown' });
    expect(state.route_readiness.reasons).toEqual(['replay_source_not_authoritative']);

    state = productStateReducer(state, {
      type: 'observatory_updated',
      generation: 6,
      source_mode: 'live',
    });
    expect(state.route_readiness.value).toBe(false);
    expect(state.route_readiness.reasons).toEqual(['qualification_unavailable']);
  });

  it('keeps blocked reason codes from rejected qualifier evidence', () => {
    const readiness = routeReadinessFromQualification(
      qualification(false),
      1_800_000_000_001,
      'live',
    );
    expect(readiness).toEqual({
      authority: PRODUCT_QUALIFIER_AUTHORITY,
      value: false,
      status: 'blocked',
      reasons: ['physical_qualification_missing'],
    });
  });

  it('navigates without changing evidence and normalizes unknown metrics to null', () => {
    const initial = createInitialProductState();
    const next = productStateReducer(initial, { type: 'navigated', route: 'plans' });
    expect(next.active_route).toBe('plans');
    expect(next.route_readiness).toBe(initial.route_readiness);
    expect(normalizeOptionalMetric(undefined)).toBeNull();
    expect(normalizeOptionalMetric(Number.NaN)).toBeNull();
    expect(normalizeOptionalMetric(-1)).toBeNull();
    expect(normalizeOptionalMetric(7)).toBe(7);
  });
});
