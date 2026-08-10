import type {
  InferenceAcceptedResponse,
  ProductQualification,
  QualificationBinding,
} from '../../app/contracts';

export type QualificationLoadStatus = 'loading' | 'ready' | 'unavailable';

export type InferencePhase =
  | 'idle'
  | 'submitting'
  | 'streaming'
  | 'interrupted'
  | 'cancelling'
  | 'completed'
  | 'cancelled'
  | 'failed';

export interface WorkloadAttribution {
  readonly profile_id: string;
  readonly qos_class: 'interactive' | 'batch';
  readonly planner_policy_id: 'balanced' | 'decode_tpot' | 'prefill_ttft';
  readonly attribution_scope: 'client_visible_planner_intent';
}

export interface InferenceHistoryEntry {
  readonly request_id: string;
  readonly prompt: string;
  readonly response: string;
  readonly terminal_state: 'completed' | 'cancelled' | 'failed';
  readonly token_count: number;
  readonly started_at_unix_ms: number;
  readonly finished_at_unix_ms: number;
  readonly deployment_id: string;
  readonly model_id: string;
  readonly error_code: string | null;
  readonly workload_attribution?: WorkloadAttribution;
}

export interface InferenceSessionState {
  readonly qualification_status: QualificationLoadStatus;
  readonly qualification: ProductQualification | null;
  readonly qualification_changed: boolean;
  readonly phase: InferencePhase;
  readonly accepted_request: InferenceAcceptedResponse | null;
  readonly captured_binding: QualificationBinding | null;
  readonly requested_max_new_tokens: number;
  readonly submitted_prompt: string | null;
  readonly output: string;
  readonly token_count: number;
  readonly last_applied_sequence: number;
  readonly error_code: string | null;
  readonly form_error: string | null;
  readonly cancellation_requested: boolean;
  readonly started_at_unix_ms: number | null;
  readonly history: readonly InferenceHistoryEntry[];
  readonly captured_workload_attribution?: WorkloadAttribution | null;
}

export const TERMINAL_INFERENCE_PHASES = new Set<InferencePhase>([
  'completed',
  'cancelled',
  'failed',
]);

export function isTerminalInferencePhase(phase: InferencePhase): boolean {
  return TERMINAL_INFERENCE_PHASES.has(phase);
}
