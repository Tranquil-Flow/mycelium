export const M15_COMPARISON_PATH = '/__mycelium/m15-plan-comparison';
export const M15_COMPARISON_PROTOCOL = 'mycelium.m15_plan_comparison.v1' as const;

export interface M15ScenarioProfile {
  readonly scenario_id: string;
  readonly prompt_p50_tokens: number;
  readonly prompt_p95_tokens: number;
  readonly output_p50_tokens: number;
  readonly output_p95_tokens: number;
  readonly modeled_concurrency: number;
  readonly batch_size: number;
  readonly qos_class: 'interactive' | 'batch';
  readonly arrival_rate_rps: number | null;
  readonly probability: number | null;
}

export interface M15WorkloadProfile {
  readonly profile_id: string;
  readonly source: string;
  readonly trace_digest: string;
  readonly trace_sample_count: number;
  readonly content_removed: true;
  readonly mode: 'probability' | 'sensitivity_grid';
  readonly arrival_process: string;
  readonly scenarios: readonly M15ScenarioProfile[];
  readonly profile_digest: string;
}

export interface M15ScenarioPrediction {
  readonly scenario_id: string;
  readonly ttft_ms: number;
  readonly prefill_compute_ms: number;
  readonly prefill_transfer_ms: number;
  readonly tpot_ms: number;
  readonly decode_compute_ms: number;
  readonly decode_transfer_ms: number;
  readonly output_goodput_tps: number;
  readonly expected_response_ms: number;
  readonly required_memory_bytes: number;
  readonly confidence: number;
}

export interface M15Candidate {
  readonly candidate_id: string;
  readonly policy_id: 'balanced' | 'decode_tpot' | 'prefill_ttft';
  readonly objective: 'balanced' | 'decode_tpot' | 'prefill_ttft';
  readonly selected: boolean;
  readonly pareto: boolean;
  readonly allocation: readonly { readonly node_id: string; readonly start: number; readonly end: number }[];
  readonly scenarios: readonly M15ScenarioPrediction[];
  readonly worst_normalized_regret: number;
  readonly worst_regret_scenario_id: string;
  readonly worst_regret_metric: string;
  readonly deltas_from_selected: readonly {
    readonly scenario_id: string;
    readonly ttft_ms: number;
    readonly tpot_ms: number;
    readonly output_goodput_tps: number;
  }[];
}

export interface M15Comparison {
  readonly profile_id: string;
  readonly selection_mode: 'minimax_normalized_regret';
  readonly selected_candidate_id: string;
  readonly winning_scenario_id: string;
  readonly winning_metric: string;
  readonly pareto_candidate_ids: readonly string[];
  readonly candidates: readonly M15Candidate[];
}

export interface M15CalibrationObservation {
  readonly profile_id: string;
  readonly request_id: string;
  readonly selected_candidate_id: string;
  readonly context_tokens: number;
  readonly output_tokens: number;
  readonly runtime_backends: readonly string[];
  readonly topology_version: number;
  readonly placement: readonly { readonly node_id: string; readonly start: number; readonly end: number }[];
  readonly counters_before: Readonly<Record<'frames_sent' | 'frames_received' | 'applied_operation_count', number>>;
  readonly counters_after: Readonly<Record<'frames_sent' | 'frames_received' | 'applied_operation_count', number>>;
  readonly prediction: Readonly<Record<'scenario_id' | 'ttft_ms' | 'tpot_ms' | 'output_goodput_tps', string | number>>;
  readonly observed: Readonly<Record<'ttft_ms' | 'tpot_ms' | 'output_goodput_tps', number>>;
  readonly signed_error: Readonly<Record<'ttft_ms' | 'tpot_ms' | 'output_goodput_tps', number>>;
  readonly absolute_relative_error: Readonly<Record<'ttft' | 'tpot' | 'throughput', number>>;
  readonly budget_results: Readonly<Record<string, string>>;
  readonly overall_state: 'met' | 'failed';
}

export interface M15PlanComparison {
  readonly protocol: typeof M15_COMPARISON_PROTOCOL;
  readonly planner_snapshot_digest: string;
  readonly evidence_bundle_digest: string;
  readonly profiles: readonly M15WorkloadProfile[];
  readonly comparisons: readonly M15Comparison[];
  readonly performance_budgets: readonly Readonly<Record<string, unknown>>[];
  readonly observations: readonly M15CalibrationObservation[];
  readonly calibration_state: 'predicted_unobserved' | 'partially_observed' | 'observed';
  readonly deferred_to_m16: readonly ['admission_latency', 'batch_shape', 'concurrency', 'queueing'];
  readonly route_ready: false;
  readonly claim_boundary: string;
}

const SHA256 = /^sha256:[0-9a-f]{64}$/;
const POLICIES = new Set(['balanced', 'decode_tpot', 'prefill_ttft']);
const TOP_LEVEL = [
  'protocol', 'planner_snapshot_digest', 'evidence_bundle_digest', 'profiles',
  'comparisons', 'performance_budgets', 'observations', 'calibration_state',
  'deferred_to_m16', 'route_ready', 'claim_boundary',
] as const;

function record(value: unknown, fields: readonly string[], path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError(`${path} must be an object`);
  }
  const keys = Object.keys(value);
  if (keys.length !== fields.length || keys.some((key) => !fields.includes(key))) {
    throw new TypeError(`${path} has unknown or missing fields`);
  }
  return value as Record<string, unknown>;
}

function boundedArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 256) {
    throw new TypeError(`${path} must be a bounded non-empty array`);
  }
  return value;
}

function finite(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new TypeError(`${path} must be a non-negative finite number`);
  }
  return value;
}

function signedFinite(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new TypeError(`${path} must be finite`);
  return value;
}

function text(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.length < 1 || value.length > 512) {
    throw new TypeError(`${path} must be bounded text`);
  }
  return value;
}

function digest(value: unknown, path: string): string {
  if (typeof value !== 'string' || !SHA256.test(value)) throw new TypeError(`${path} must be a digest`);
  return value;
}

export function decodeM15PlanComparison(value: unknown): M15PlanComparison {
  const document = record(value, TOP_LEVEL, 'm15_comparison');
  if (document.protocol !== M15_COMPARISON_PROTOCOL || document.route_ready !== false) {
    throw new TypeError('m15_comparison authority is invalid');
  }
  if (!['predicted_unobserved', 'partially_observed', 'observed'].includes(String(document.calibration_state))) {
    throw new TypeError('m15_comparison calibration state is invalid');
  }
  const deferred = boundedArray(document.deferred_to_m16, 'm15_comparison.deferred_to_m16');
  if (deferred.join(',') !== 'admission_latency,batch_shape,concurrency,queueing') {
    throw new TypeError('m15_comparison deferred boundary is invalid');
  }
  const profiles = boundedArray(document.profiles, 'm15_comparison.profiles').map((source, index) => {
    const path = `m15_comparison.profiles[${index}]`;
    const item = record(source, [
      'profile_id', 'source', 'trace_digest', 'trace_sample_count', 'content_removed',
      'mode', 'arrival_process', 'scenarios', 'profile_digest',
    ], path);
    if (item.content_removed !== true || !['probability', 'sensitivity_grid'].includes(String(item.mode))) {
      throw new TypeError(`${path} profile authority is invalid`);
    }
    const scenarios = boundedArray(item.scenarios, `${path}.scenarios`).map((raw, scenarioIndex) => {
      const scenarioPath = `${path}.scenarios[${scenarioIndex}]`;
      const scenario = record(raw, [
        'scenario_id', 'prompt_p50_tokens', 'prompt_p95_tokens', 'output_p50_tokens',
        'output_p95_tokens', 'modeled_concurrency', 'batch_size', 'qos_class',
        'arrival_rate_rps', 'probability',
      ], scenarioPath);
      if (!['interactive', 'batch'].includes(String(scenario.qos_class))) throw new TypeError(`${scenarioPath}.qos_class is invalid`);
      return Object.freeze({
        scenario_id: text(scenario.scenario_id, `${scenarioPath}.scenario_id`),
        prompt_p50_tokens: finite(scenario.prompt_p50_tokens, `${scenarioPath}.prompt_p50_tokens`),
        prompt_p95_tokens: finite(scenario.prompt_p95_tokens, `${scenarioPath}.prompt_p95_tokens`),
        output_p50_tokens: finite(scenario.output_p50_tokens, `${scenarioPath}.output_p50_tokens`),
        output_p95_tokens: finite(scenario.output_p95_tokens, `${scenarioPath}.output_p95_tokens`),
        modeled_concurrency: finite(scenario.modeled_concurrency, `${scenarioPath}.modeled_concurrency`),
        batch_size: finite(scenario.batch_size, `${scenarioPath}.batch_size`),
        qos_class: scenario.qos_class as 'interactive' | 'batch',
        arrival_rate_rps: scenario.arrival_rate_rps === null ? null : finite(scenario.arrival_rate_rps, `${scenarioPath}.arrival_rate_rps`),
        probability: scenario.probability === null ? null : finite(scenario.probability, `${scenarioPath}.probability`),
      });
    });
    return Object.freeze({
      profile_id: text(item.profile_id, `${path}.profile_id`), source: text(item.source, `${path}.source`),
      trace_digest: digest(item.trace_digest, `${path}.trace_digest`),
      trace_sample_count: finite(item.trace_sample_count, `${path}.trace_sample_count`), content_removed: true as const,
      mode: item.mode as M15WorkloadProfile['mode'], arrival_process: text(item.arrival_process, `${path}.arrival_process`),
      scenarios: Object.freeze(scenarios), profile_digest: digest(item.profile_digest, `${path}.profile_digest`),
    });
  });
  const comparisons = boundedArray(document.comparisons, 'm15_comparison.comparisons').map((source, index) => {
    const path = `m15_comparison.comparisons[${index}]`;
    const item = record(source, [
      'profile_id', 'selection_mode', 'selected_candidate_id', 'winning_scenario_id',
      'winning_metric', 'pareto_candidate_ids', 'candidates',
    ], path);
    if (item.selection_mode !== 'minimax_normalized_regret') throw new TypeError(`${path}.selection_mode is invalid`);
    const candidates = boundedArray(item.candidates, `${path}.candidates`).map((raw, candidateIndex) => {
      const candidatePath = `${path}.candidates[${candidateIndex}]`;
      const candidate = record(raw, [
        'candidate_id', 'policy_id', 'objective', 'selected', 'pareto', 'allocation',
        'scenarios', 'worst_normalized_regret', 'worst_regret_scenario_id',
        'worst_regret_metric', 'deltas_from_selected',
      ], candidatePath);
      if (!POLICIES.has(String(candidate.policy_id)) || candidate.objective !== candidate.policy_id || typeof candidate.selected !== 'boolean' || typeof candidate.pareto !== 'boolean') {
        throw new TypeError(`${candidatePath} policy authority is invalid`);
      }
      const allocation = boundedArray(candidate.allocation, `${candidatePath}.allocation`).map((rawAllocation, allocationIndex) => {
        const allocationPath = `${candidatePath}.allocation[${allocationIndex}]`;
        const entry = record(rawAllocation, ['node_id', 'start', 'end'], allocationPath);
        const start = finite(entry.start, `${allocationPath}.start`);
        const end = finite(entry.end, `${allocationPath}.end`);
        if (end <= start) throw new TypeError(`${allocationPath} has an empty range`);
        return Object.freeze({ node_id: text(entry.node_id, `${allocationPath}.node_id`), start, end });
      });
      const scenarios = boundedArray(candidate.scenarios, `${candidatePath}.scenarios`).map((rawScenario, scenarioIndex) => {
        const scenarioPath = `${candidatePath}.scenarios[${scenarioIndex}]`;
        const scenario = record(rawScenario, [
          'scenario_id', 'ttft_ms', 'prefill_compute_ms', 'prefill_transfer_ms',
          'tpot_ms', 'decode_compute_ms', 'decode_transfer_ms', 'output_goodput_tps',
          'expected_response_ms', 'required_memory_bytes', 'confidence',
        ], scenarioPath);
        return Object.freeze({
          scenario_id: text(scenario.scenario_id, `${scenarioPath}.scenario_id`),
          ttft_ms: finite(scenario.ttft_ms, `${scenarioPath}.ttft_ms`),
          prefill_compute_ms: finite(scenario.prefill_compute_ms, `${scenarioPath}.prefill_compute_ms`),
          prefill_transfer_ms: finite(scenario.prefill_transfer_ms, `${scenarioPath}.prefill_transfer_ms`),
          tpot_ms: finite(scenario.tpot_ms, `${scenarioPath}.tpot_ms`),
          decode_compute_ms: finite(scenario.decode_compute_ms, `${scenarioPath}.decode_compute_ms`),
          decode_transfer_ms: finite(scenario.decode_transfer_ms, `${scenarioPath}.decode_transfer_ms`),
          output_goodput_tps: finite(scenario.output_goodput_tps, `${scenarioPath}.output_goodput_tps`),
          expected_response_ms: finite(scenario.expected_response_ms, `${scenarioPath}.expected_response_ms`),
          required_memory_bytes: finite(scenario.required_memory_bytes, `${scenarioPath}.required_memory_bytes`),
          confidence: finite(scenario.confidence, `${scenarioPath}.confidence`),
        });
      });
      const deltas = boundedArray(candidate.deltas_from_selected, `${candidatePath}.deltas_from_selected`).map((rawDelta, deltaIndex) => {
        const deltaPath = `${candidatePath}.deltas_from_selected[${deltaIndex}]`;
        const delta = record(rawDelta, ['scenario_id', 'ttft_ms', 'tpot_ms', 'output_goodput_tps'], deltaPath);
        const signed = (value: unknown, field: string) => {
          if (typeof value !== 'number' || !Number.isFinite(value)) throw new TypeError(`${field} must be finite`);
          return value;
        };
        return Object.freeze({
          scenario_id: text(delta.scenario_id, `${deltaPath}.scenario_id`),
          ttft_ms: signed(delta.ttft_ms, `${deltaPath}.ttft_ms`),
          tpot_ms: signed(delta.tpot_ms, `${deltaPath}.tpot_ms`),
          output_goodput_tps: signed(delta.output_goodput_tps, `${deltaPath}.output_goodput_tps`),
        });
      });
      return Object.freeze({
        candidate_id: text(candidate.candidate_id, `${candidatePath}.candidate_id`),
        policy_id: candidate.policy_id as M15Candidate['policy_id'],
        objective: candidate.objective as M15Candidate['objective'],
        selected: candidate.selected,
        pareto: candidate.pareto,
        allocation: Object.freeze(allocation),
        scenarios: Object.freeze(scenarios),
        worst_normalized_regret: finite(candidate.worst_normalized_regret, `${candidatePath}.worst_normalized_regret`),
        worst_regret_scenario_id: text(candidate.worst_regret_scenario_id, `${candidatePath}.worst_regret_scenario_id`),
        worst_regret_metric: text(candidate.worst_regret_metric, `${candidatePath}.worst_regret_metric`),
        deltas_from_selected: Object.freeze(deltas),
      });
    });
    if (new Set(candidates.map((candidate) => candidate.policy_id)).size !== 3 || candidates.some((candidate) => !POLICIES.has(candidate.policy_id))) {
      throw new TypeError(`${path}.candidates is incomplete`);
    }
    const selected = candidates.filter((candidate) => candidate.selected === true);
    const pareto = boundedArray(item.pareto_candidate_ids, `${path}.pareto_candidate_ids`).map((id, idIndex) => text(id, `${path}.pareto_candidate_ids[${idIndex}]`));
    if (selected.length !== 1 || selected[0].candidate_id !== item.selected_candidate_id || !pareto.includes(String(item.selected_candidate_id))) {
      throw new TypeError(`${path}.selection is invalid`);
    }
    return Object.freeze({
      profile_id: text(item.profile_id, `${path}.profile_id`), selection_mode: 'minimax_normalized_regret' as const,
      selected_candidate_id: text(item.selected_candidate_id, `${path}.selected_candidate_id`),
      winning_scenario_id: text(item.winning_scenario_id, `${path}.winning_scenario_id`),
      winning_metric: text(item.winning_metric, `${path}.winning_metric`), pareto_candidate_ids: Object.freeze(pareto),
      candidates: Object.freeze(candidates),
    });
  });
  if (profiles.map((item) => item.profile_id).join(',') !== comparisons.map((item) => item.profile_id).join(',')) {
    throw new TypeError('m15_comparison profile binding is invalid');
  }
  const performanceBudgets = Array.isArray(document.performance_budgets)
    ? document.performance_budgets.map((source, index) => Object.freeze(record(source, [
        'protocol', 'budget_id', 'profile_id', 'minimum_sample_size', 'ttft_ms_maximum',
        'tpot_ms_maximum', 'minimum_output_tokens_per_second', 'maximum_frames_per_request',
        'maximum_relative_model_error', 'execution_scope', 'peak_memory_budget_state',
        'energy_thermal_budget_state', 'reconnect_budget_state', 'queueing_budget_state',
        'admission_latency_budget_state', 'concurrency_budget_state', 'batch_shape_budget_state',
      ], `m15_comparison.performance_budgets[${index}]`)))
    : (() => { throw new TypeError('m15_comparison.performance_budgets must be an array'); })();
  const observations = Array.isArray(document.observations)
    ? document.observations.map((source, index) => {
        const path = `m15_comparison.observations[${index}]`;
        const item = record(source, [
          'profile_id', 'request_id', 'selected_candidate_id', 'context_tokens', 'output_tokens',
          'runtime_backends', 'topology_version', 'placement', 'counters_before', 'counters_after',
          'prediction', 'observed', 'signed_error', 'absolute_relative_error', 'budget_results',
          'overall_state',
        ], path);
        const placement = boundedArray(item.placement, `${path}.placement`).map((raw, placementIndex) => {
          const entryPath = `${path}.placement[${placementIndex}]`;
          const entry = record(raw, ['node_id', 'start', 'end'], entryPath);
          return Object.freeze({ node_id: text(entry.node_id, `${entryPath}.node_id`), start: finite(entry.start, `${entryPath}.start`), end: finite(entry.end, `${entryPath}.end`) });
        });
        const counter = (raw: unknown, counterPath: string) => {
          const value = record(raw, ['frames_sent', 'frames_received', 'applied_operation_count'], counterPath);
          return Object.freeze({ frames_sent: finite(value.frames_sent, `${counterPath}.frames_sent`), frames_received: finite(value.frames_received, `${counterPath}.frames_received`), applied_operation_count: finite(value.applied_operation_count, `${counterPath}.applied_operation_count`) });
        };
        const metric = (raw: unknown, metricPath: string) => {
          const value = record(raw, ['ttft_ms', 'tpot_ms', 'output_goodput_tps'], metricPath);
          return Object.freeze({ ttft_ms: finite(value.ttft_ms, `${metricPath}.ttft_ms`), tpot_ms: finite(value.tpot_ms, `${metricPath}.tpot_ms`), output_goodput_tps: finite(value.output_goodput_tps, `${metricPath}.output_goodput_tps`) });
        };
        const prediction = record(item.prediction, ['scenario_id', 'ttft_ms', 'tpot_ms', 'output_goodput_tps'], `${path}.prediction`);
        const signedError = record(item.signed_error, ['ttft_ms', 'tpot_ms', 'output_goodput_tps'], `${path}.signed_error`);
        const relativeError = record(item.absolute_relative_error, ['ttft', 'tpot', 'throughput'], `${path}.absolute_relative_error`);
        if (!Array.isArray(item.runtime_backends) || item.runtime_backends.length < 1 || !['met', 'failed'].includes(String(item.overall_state))) throw new TypeError(`${path} authority is invalid`);
        const budgetResults = item.budget_results;
        if (typeof budgetResults !== 'object' || budgetResults === null || Array.isArray(budgetResults)) throw new TypeError(`${path}.budget_results is invalid`);
        return Object.freeze({
          profile_id: text(item.profile_id, `${path}.profile_id`), request_id: text(item.request_id, `${path}.request_id`),
          selected_candidate_id: text(item.selected_candidate_id, `${path}.selected_candidate_id`),
          context_tokens: finite(item.context_tokens, `${path}.context_tokens`), output_tokens: finite(item.output_tokens, `${path}.output_tokens`),
          runtime_backends: Object.freeze(item.runtime_backends.map((value, backendIndex) => text(value, `${path}.runtime_backends[${backendIndex}]`))),
          topology_version: finite(item.topology_version, `${path}.topology_version`), placement: Object.freeze(placement),
          counters_before: counter(item.counters_before, `${path}.counters_before`), counters_after: counter(item.counters_after, `${path}.counters_after`),
          prediction: Object.freeze({ scenario_id: text(prediction.scenario_id, `${path}.prediction.scenario_id`), ttft_ms: finite(prediction.ttft_ms, `${path}.prediction.ttft_ms`), tpot_ms: finite(prediction.tpot_ms, `${path}.prediction.tpot_ms`), output_goodput_tps: finite(prediction.output_goodput_tps, `${path}.prediction.output_goodput_tps`) }),
          observed: metric(item.observed, `${path}.observed`),
          signed_error: Object.freeze({ ttft_ms: signedFinite(signedError.ttft_ms, `${path}.signed_error.ttft_ms`), tpot_ms: signedFinite(signedError.tpot_ms, `${path}.signed_error.tpot_ms`), output_goodput_tps: signedFinite(signedError.output_goodput_tps, `${path}.signed_error.output_goodput_tps`) }),
          absolute_relative_error: Object.freeze({ ttft: finite(relativeError.ttft, `${path}.absolute_relative_error.ttft`), tpot: finite(relativeError.tpot, `${path}.absolute_relative_error.tpot`), throughput: finite(relativeError.throughput, `${path}.absolute_relative_error.throughput`) }),
          budget_results: Object.freeze(Object.fromEntries(Object.entries(budgetResults).map(([key, value]) => [key, text(value, `${path}.budget_results.${key}`)]))),
          overall_state: item.overall_state as 'met' | 'failed',
        });
      })
    : (() => { throw new TypeError('m15_comparison.observations must be an array'); })();
  if (document.calibration_state === 'predicted_unobserved' && (performanceBudgets.length > 0 || observations.length > 0)) {
    throw new TypeError('m15_comparison unobserved state contains calibration');
  }
  if (document.calibration_state === 'observed' && observations.length !== profiles.length) {
    throw new TypeError('m15_comparison observed profile binding is invalid');
  }
  return Object.freeze({
    protocol: M15_COMPARISON_PROTOCOL,
    planner_snapshot_digest: digest(document.planner_snapshot_digest, 'm15_comparison.planner_snapshot_digest'),
    evidence_bundle_digest: digest(document.evidence_bundle_digest, 'm15_comparison.evidence_bundle_digest'),
    profiles: Object.freeze(profiles), comparisons: Object.freeze(comparisons),
    performance_budgets: Object.freeze(performanceBudgets), observations: Object.freeze(observations),
    calibration_state: document.calibration_state as M15PlanComparison['calibration_state'],
    deferred_to_m16: Object.freeze(deferred) as unknown as M15PlanComparison['deferred_to_m16'],
    route_ready: false, claim_boundary: text(document.claim_boundary, 'm15_comparison.claim_boundary'),
  });
}

export interface M15ComparisonClient { load(): Promise<M15PlanComparison> }

export class HttpM15ComparisonClient implements M15ComparisonClient {
  readonly #fetcher: typeof fetch;
  constructor(fetcher: typeof fetch = globalThis.fetch.bind(globalThis)) { this.#fetcher = fetcher; }
  async load(): Promise<M15PlanComparison> {
    const response = await this.#fetcher(M15_COMPARISON_PATH, { method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`m15_comparison_${response.status}`);
    return decodeM15PlanComparison(await response.json());
  }
}
