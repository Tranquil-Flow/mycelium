import { describe, expect, it } from 'vitest';
import { decodeM19Liveness, decodeM19RecoveryPlan, decodeM19RecoveryRuntime } from './m19Recovery';

const sha = (character: string) => `sha256:${character.repeat(64)}`;
const binding = { deployment_id: 'deployment-m19', deployment_epoch: 19, topology_version: 7, model_id: 'Qwen/Qwen2.5-0.5B-Instruct', model_revision: 'a'.repeat(40), representation_digest: sha('a'), graph_digest: sha('b'), membership_generation: 4 };
export const livenessFixture = () => ({ protocol: 'mycelium.m19_liveness.v1', generated_at_unix_ms: 16000, binding, budgets: { active_failure_detection_ms: 2000, idle_keepalive_ms: 5000, suspect_misses: 2, quarantine_misses: 3, quarantine_stale_ms: 15000, recovery_fresh_observations: 2 }, subjects: [{ subject_id: 'peer-a', state: 'failed', last_fresh_unix_ms: 1000, last_observed_unix_ms: 16000, consecutive_misses: 1, consecutive_fresh: 0 }], incidents: [{ incident_id: 'm19-incident-1', subject_id: 'peer-a', scope: 'placement', detector_source: 'traffic_aware_liveness', reason: 'active_disconnect', first_observed_unix_ms: 16000, last_observed_unix_ms: 16000, old_generation: 4, new_generation: 4, affected_track_ids: ['track-primary'], action: 'remove_from_admission', terminal_outcome: 'failed' }], evidence_digest: sha('c') });
export const planFixture = () => ({ protocol: 'mycelium.m19_recovery_plan.v1', generated_at_unix_ms: 16000, binding, incumbent_track_ids: ['track-primary', 'track-replica'], failed_track_ids: ['track-primary'], surviving_track_ids: ['track-replica'], successors: [{ track_id: 'track-replica', qualification_id: 'qualification-replica', qualification_digest: sha('d'), decode_mode: 'stage_local_kv', kv_compatibility: 'compatible', kv_schema_digest: sha('e'), failure_domain: 'host-b' }], candidate_state: 'hysteresis_pending', equivalent_candidate_generations: 2, candidate_first_seen_unix_ms: 1000, provisioning_allowed: false, claim_boundary: 'planner intent only', plan_digest: sha('f') });
export const runtimeFixture = () => ({ protocol: 'mycelium.m19_recovery_runtime.v1', binding, maximum_recovery_attempts: 2, requests: [{ request_id: 'request-a', attempt: 2, path_id: 'path-b', track_id: 'track-replica', qualification_id: 'qualification-replica', qualification_digest: sha('d'), committed_token_count: 4, committed_token_digest: sha('a'), recovery_mode: 'full_context_replay', successor_track_id: 'track-replica', successor_path_id: 'path-b', kv_outcome: 'not_transferred', replay_performed: true, terminal_state: 'completed', terminal_reason: 'completed', cleanup_complete: true }, { request_id: 'request-b', attempt: 1, path_id: 'path-a', track_id: 'track-primary', qualification_id: 'qualification-primary', qualification_digest: sha('b'), committed_token_count: 2, committed_token_digest: sha('c'), recovery_mode: 'none', successor_track_id: null, successor_path_id: null, kv_outcome: 'not_applicable', replay_performed: false, terminal_state: 'aborted', terminal_reason: 'no_compatible_successor', cleanup_complete: true }], breaker: { state: 'closed', failure_observations_unix_ms: [], open_until_unix_ms: 0 }, reconciliation: { 'request-a': 'already_terminal' }, runtime_digest: sha('9') });

describe('M19 recovery decoders', () => {
  it('decodes scoped liveness, successor intent, replay, and truthful abort', () => {
    expect(decodeM19Liveness(livenessFixture()).incidents[0].scope).toBe('placement');
    expect(decodeM19RecoveryPlan(planFixture()).surviving_track_ids).toEqual(['track-replica']);
    const runtime = decodeM19RecoveryRuntime(runtimeFixture());
    expect(runtime.requests[0].kv_outcome).toBe('not_transferred');
    expect(runtime.requests[1].terminal_state).toBe('aborted');
  });
  it('rejects unknown and private browser fields', () => {
    expect(() => decodeM19Liveness({ ...livenessFixture(), hostname: 'private' })).toThrow(/unknown or missing/);
    const runtime = runtimeFixture();
    expect(() => decodeM19RecoveryRuntime({ ...runtime, requests: [{ ...runtime.requests[0], prompt: 'private' }] })).toThrow(/unknown or missing/);
  });
});
