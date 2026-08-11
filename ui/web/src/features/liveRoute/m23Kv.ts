export type M23KvEvidence = Readonly<{
  protocol: 'mycelium.m23_heterogeneous_kv_gate.v1';
  generated_at_unix_ms: number;
  replay_capture_digest: string;
  kv_capture_digest: string;
  gates: Readonly<{
    same_route_model_stages_hosts: boolean;
    same_prompt_and_budget: boolean;
    exact_output_parity: boolean;
    one_token_decode_every_stage: boolean;
    all_stages_advanced_physical_counters: boolean;
    kv_active_then_terminally_released: boolean;
    no_fatal_or_cleanup_failure: boolean;
    measured_tpot_improvement: boolean;
  }>;
  implemented: boolean;
  performance_qualified: boolean;
  promotion_state: 'qualified' | 'implemented_not_performance_qualified' | 'withheld';
  measurements: Readonly<{
    replay_tpot_ms: number;
    kv_tpot_ms: number;
    tpot_delta_ms: number;
    tpot_improvement_ratio: number;
    replay_activation_output_bytes: number;
    kv_activation_output_bytes: number;
    activation_byte_delta: number;
    replay_total_ms: number;
    kv_total_ms: number;
  }>;
  claim_boundary: string;
  evidence_digest: string;
}>;

function exact(value: unknown, fields: readonly string[], label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new TypeError(`${label} must be an object`);
  const record = value as Record<string, unknown>;
  if (Object.keys(record).sort().join('\0') !== [...fields].sort().join('\0')) throw new TypeError(`${label} has unknown or missing fields`);
  return record;
}
function finite(value: unknown, label: string): number { if (typeof value !== 'number' || !Number.isFinite(value)) throw new TypeError(`${label} is invalid`); return value; }
function integer(value: unknown, label: string): number { const result = finite(value, label); if (!Number.isSafeInteger(result) || result < 0) throw new TypeError(`${label} is invalid`); return result; }
function bool(value: unknown, label: string): boolean { if (typeof value !== 'boolean') throw new TypeError(`${label} is invalid`); return value; }
function text(value: unknown, label: string): string { if (typeof value !== 'string' || value.length === 0 || value.length > 512) throw new TypeError(`${label} is invalid`); return value; }
function sha(value: unknown, label: string): string { const result = text(value, label); if (!/^sha256:[0-9a-f]{64}$/.test(result)) throw new TypeError(`${label} is invalid`); return result; }

export function decodeM23KvEvidence(value: unknown): M23KvEvidence {
  const root = exact(value, ['protocol','generated_at_unix_ms','replay_capture_digest','kv_capture_digest','gates','implemented','performance_qualified','promotion_state','measurements','claim_boundary','evidence_digest'], 'm23 kv');
  if (root.protocol !== 'mycelium.m23_heterogeneous_kv_gate.v1' || !['qualified','implemented_not_performance_qualified','withheld'].includes(String(root.promotion_state))) throw new TypeError('m23 kv protocol or promotion is invalid');
  const gates = exact(root.gates, ['same_route_model_stages_hosts','same_prompt_and_budget','exact_output_parity','one_token_decode_every_stage','all_stages_advanced_physical_counters','kv_active_then_terminally_released','no_fatal_or_cleanup_failure','measured_tpot_improvement'], 'm23 gates');
  const measurements = exact(root.measurements, ['replay_tpot_ms','kv_tpot_ms','tpot_delta_ms','tpot_improvement_ratio','replay_activation_output_bytes','kv_activation_output_bytes','activation_byte_delta','replay_total_ms','kv_total_ms'], 'm23 measurements');
  return Object.freeze({
    protocol: 'mycelium.m23_heterogeneous_kv_gate.v1',
    generated_at_unix_ms: integer(root.generated_at_unix_ms, 'generated'),
    replay_capture_digest: sha(root.replay_capture_digest, 'replay digest'),
    kv_capture_digest: sha(root.kv_capture_digest, 'kv digest'),
    gates: Object.freeze({
      same_route_model_stages_hosts: bool(gates.same_route_model_stages_hosts, 'same route'),
      same_prompt_and_budget: bool(gates.same_prompt_and_budget, 'same prompt'),
      exact_output_parity: bool(gates.exact_output_parity, 'output parity'),
      one_token_decode_every_stage: bool(gates.one_token_decode_every_stage, 'one token decode'),
      all_stages_advanced_physical_counters: bool(gates.all_stages_advanced_physical_counters, 'physical counters'),
      kv_active_then_terminally_released: bool(gates.kv_active_then_terminally_released, 'kv release'),
      no_fatal_or_cleanup_failure: bool(gates.no_fatal_or_cleanup_failure, 'fatal cleanup'),
      measured_tpot_improvement: bool(gates.measured_tpot_improvement, 'tpot improvement'),
    }),
    implemented: bool(root.implemented, 'implemented'),
    performance_qualified: bool(root.performance_qualified, 'performance qualified'),
    promotion_state: root.promotion_state as M23KvEvidence['promotion_state'],
    measurements: Object.freeze({
      replay_tpot_ms: finite(measurements.replay_tpot_ms, 'replay tpot'),
      kv_tpot_ms: finite(measurements.kv_tpot_ms, 'kv tpot'),
      tpot_delta_ms: finite(measurements.tpot_delta_ms, 'tpot delta'),
      tpot_improvement_ratio: finite(measurements.tpot_improvement_ratio, 'improvement ratio'),
      replay_activation_output_bytes: integer(measurements.replay_activation_output_bytes, 'replay activation'),
      kv_activation_output_bytes: integer(measurements.kv_activation_output_bytes, 'kv activation'),
      activation_byte_delta: finite(measurements.activation_byte_delta, 'activation delta'),
      replay_total_ms: finite(measurements.replay_total_ms, 'replay total'),
      kv_total_ms: finite(measurements.kv_total_ms, 'kv total'),
    }),
    claim_boundary: text(root.claim_boundary, 'claim boundary'),
    evidence_digest: sha(root.evidence_digest, 'evidence digest'),
  });
}
