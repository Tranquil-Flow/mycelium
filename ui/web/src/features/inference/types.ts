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

export interface InferenceHistoryEntry {
  readonly request_id: string;
  readonly terminal_state: 'completed' | 'cancelled' | 'failed';
  readonly token_count: number;
  readonly started_at_unix_ms: number;
  readonly finished_at_unix_ms: number;
  readonly deployment_id: string;
  readonly model_id: string;
  readonly error_code: string | null;
}

export interface InferenceSessionState {
  readonly qualification_status: QualificationLoadStatus;
  readonly qualification: ProductQualification | null;
  readonly qualification_changed: boolean;
  readonly phase: InferencePhase;
  readonly accepted_request: InferenceAcceptedResponse | null;
  readonly captured_binding: QualificationBinding | null;
  readonly requested_max_new_tokens: number;
  readonly output: string;
  readonly token_count: number;
  readonly last_applied_sequence: number;
  readonly error_code: string | null;
  readonly form_error: string | null;
  readonly cancellation_requested: boolean;
  readonly started_at_unix_ms: number | null;
  readonly history: readonly InferenceHistoryEntry[];
}

export const TERMINAL_INFERENCE_PHASES = new Set<InferencePhase>([
  'completed',
  'cancelled',
  'failed',
]);

export function isTerminalInferencePhase(phase: InferencePhase): boolean {
  return TERMINAL_INFERENCE_PHASES.has(phase);
}
