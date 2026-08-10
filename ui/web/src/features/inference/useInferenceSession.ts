import { useCallback, useEffect, useReducer, useRef } from 'react';
import {
  ProductContractError,
  buildInferenceSubmission,
  inferenceBlockReason,
  type InferenceAcceptedResponse,
  type InferenceEvent,
  type InferenceSubmission,
  type ProductQualification,
  type QualificationBinding,
} from '../../app/contracts';
import {
  InferenceClientError,
  type InferenceClient,
} from './requestClient';
import {
  isTerminalInferencePhase,
  type InferenceHistoryEntry,
  type InferenceSessionState,
  type WorkloadAttribution,
} from './types';

const QUALIFICATION_CHANGED_REASON =
  'Qualification changed; review and accept the current binding';
const ACTIVE_REQUEST_REASON = 'A request is already active';
const RESTORED_STREAM_RETRY_DELAYS_MS = Object.freeze([250, 750, 1_500, 3_000]);
const MAX_HISTORY_PROMPT_CHARS = 8_192;
const MAX_HISTORY_RESPONSE_CHARS = 65_536;

interface UseInferenceSessionOptions {
  readonly client: InferenceClient;
  readonly now?: () => number;
  readonly restored_state?: InferenceSessionState | null;
}

type SessionAction =
  | { readonly type: 'qualification_loaded'; readonly value: ProductQualification; readonly changed: boolean }
  | { readonly type: 'qualification_unavailable' }
  | { readonly type: 'qualification_accepted' }
  | {
      readonly type: 'submitting';
      readonly binding: QualificationBinding;
      readonly prompt: string;
      readonly max_new_tokens: number;
      readonly now: number;
      readonly workload_attribution?: WorkloadAttribution;
    }
  | { readonly type: 'accepted'; readonly request: InferenceAcceptedResponse }
  | { readonly type: 'event'; readonly event: InferenceEvent; readonly now: number }
  | { readonly type: 'interrupted'; readonly code: string | null }
  | { readonly type: 'stream_reopened' }
  | { readonly type: 'cancelling' }
  | { readonly type: 'cancel_failed'; readonly code: string }
  | { readonly type: 'stream_failed'; readonly code: string; readonly now: number }
  | { readonly type: 'submission_failed'; readonly code: string }
  | { readonly type: 'form_failed'; readonly reason: string }
  | { readonly type: 'clear_form_error' }
  | { readonly type: 'clear_session' };

const initialState: InferenceSessionState = Object.freeze({
  qualification_status: 'loading',
  qualification: null,
  qualification_changed: false,
  phase: 'idle',
  accepted_request: null,
  captured_binding: null,
  requested_max_new_tokens: 0,
  submitted_prompt: null,
  output: '',
  token_count: 0,
  last_applied_sequence: -1,
  error_code: null,
  form_error: null,
  cancellation_requested: false,
  started_at_unix_ms: null,
  history: Object.freeze([]),
  captured_workload_attribution: null,
});

function restoreInitialState(
  restored: InferenceSessionState | null | undefined,
): InferenceSessionState {
  if (restored === null || restored === undefined) return initialState;
  const terminalHistory = restored.accepted_request === null
    ? undefined
    : restored.history.find(
        (entry) => entry.request_id === restored.accepted_request?.request_id,
      );
  const phase = terminalHistory !== undefined
    ? terminalHistory.terminal_state
    : ['submitting', 'streaming', 'cancelling'].includes(restored.phase)
      ? restored.accepted_request === null
        ? 'idle'
        : 'interrupted'
      : restored.phase;
  return Object.freeze({
    ...restored,
    qualification_status: 'loading',
    qualification: null,
    qualification_changed: false,
    phase,
    form_error: null,
    error_code: terminalHistory?.error_code ?? restored.error_code,
    cancellation_requested: false,
    history: Object.freeze([...restored.history]),
  });
}

function cloneBinding(binding: QualificationBinding): QualificationBinding {
  return Object.freeze({
    qualification_id: binding.qualification_id,
    qualification_digest: binding.qualification_digest,
    deployment_id: binding.deployment_id,
    deployment_epoch: binding.deployment_epoch,
    topology_version: binding.topology_version,
    model_id: binding.model_id,
    resolved_commit: binding.resolved_commit,
    manifest_digest: binding.manifest_digest,
    path_manifest_digest: binding.path_manifest_digest,
    stage_load_proof_digests: Object.freeze([...binding.stage_load_proof_digests]),
  });
}

function sameBinding(left: ProductQualification, right: ProductQualification): boolean {
  const a = left.binding;
  const b = right.binding;
  return (
    left.issued_at_unix_ms === right.issued_at_unix_ms &&
    left.route_ready === right.route_ready &&
    left.evidence_class === right.evidence_class &&
    left.reason_codes.length === right.reason_codes.length &&
    left.reason_codes.every((reason, index) => reason === right.reason_codes[index]) &&
    a.qualification_id === b.qualification_id &&
    a.qualification_digest === b.qualification_digest &&
    a.deployment_id === b.deployment_id &&
    a.deployment_epoch === b.deployment_epoch &&
    a.topology_version === b.topology_version &&
    a.model_id === b.model_id &&
    a.resolved_commit === b.resolved_commit &&
    a.manifest_digest === b.manifest_digest &&
    a.path_manifest_digest === b.path_manifest_digest &&
    a.stage_load_proof_digests.length === b.stage_load_proof_digests.length &&
    a.stage_load_proof_digests.every(
      (digest, index) => digest === b.stage_load_proof_digests[index],
    )
  );
}

function terminalHistory(
  state: InferenceSessionState,
  phase: 'completed' | 'cancelled' | 'failed',
  now: number,
  errorCode: string | null,
): readonly InferenceHistoryEntry[] {
  if (
    state.accepted_request === null ||
    state.captured_binding === null ||
    state.started_at_unix_ms === null ||
    state.history.some((entry) => entry.request_id === state.accepted_request?.request_id)
  ) {
    return state.history;
  }
  return Object.freeze([
    {
      request_id: state.accepted_request.request_id,
      prompt: boundedHistoryText(
        state.submitted_prompt ?? '',
        MAX_HISTORY_PROMPT_CHARS,
      ),
      response: boundedHistoryText(state.output, MAX_HISTORY_RESPONSE_CHARS),
      terminal_state: phase,
      token_count: state.token_count,
      started_at_unix_ms: state.started_at_unix_ms,
      finished_at_unix_ms: now,
      deployment_id: state.captured_binding.deployment_id,
      model_id: state.captured_binding.model_id,
      error_code: errorCode,
      ...(state.captured_workload_attribution === null || state.captured_workload_attribution === undefined
        ? {}
        : { workload_attribution: Object.freeze({ ...state.captured_workload_attribution }) }),
    },
    ...state.history,
  ].slice(0, 20));
}

function boundedHistoryText(value: string, maximum: number): string {
  return value.length <= maximum ? value : `${value.slice(0, maximum - 1)}…`;
}

function failStream(
  state: InferenceSessionState,
  code: string,
  now: number,
): InferenceSessionState {
  if (isTerminalInferencePhase(state.phase)) return state;
  return Object.freeze({
    ...state,
    phase: 'failed',
    error_code: code,
    history: terminalHistory(state, 'failed', now, code),
  });
}

export function inferenceSessionReducer(
  state: InferenceSessionState,
  action: SessionAction,
): InferenceSessionState {
  switch (action.type) {
    case 'qualification_loaded':
      return Object.freeze({
        ...state,
        qualification_status: 'ready',
        qualification: action.value,
        qualification_changed: action.changed,
        phase: action.changed && state.phase === 'submitting' ? 'idle' : state.phase,
      });
    case 'qualification_unavailable':
      return Object.freeze({
        ...state,
        qualification_status: 'unavailable',
        qualification: null,
        qualification_changed: false,
        phase: state.phase === 'submitting' ? 'idle' : state.phase,
      });
    case 'qualification_accepted':
      return state.qualification === null
        ? state
        : Object.freeze({ ...state, qualification_changed: false });
    case 'submitting':
      return Object.freeze({
        ...state,
        phase: 'submitting',
        captured_binding: cloneBinding(action.binding),
        submitted_prompt: action.prompt,
        requested_max_new_tokens: action.max_new_tokens,
        accepted_request: null,
        output: '',
        token_count: 0,
        last_applied_sequence: -1,
        error_code: null,
        form_error: null,
        cancellation_requested: false,
        started_at_unix_ms: action.now,
        captured_workload_attribution: action.workload_attribution === undefined
          ? null
          : Object.freeze({ ...action.workload_attribution }),
      });
    case 'accepted':
      return isTerminalInferencePhase(state.phase)
        ? state
        : Object.freeze({ ...state, phase: 'streaming', accepted_request: action.request });
    case 'event': {
      if (isTerminalInferencePhase(state.phase)) return state;
      if (state.accepted_request === null || action.event.request_id !== state.accepted_request.request_id) {
        return failStream(state, 'stream_request_mismatch', action.now);
      }
      if (action.event.sequence <= state.last_applied_sequence) return state;
      if (action.event.sequence !== state.last_applied_sequence + 1) {
        return failStream(state, 'stream_sequence_invalid', action.now);
      }
      if (action.event.type === 'accepted') {
        if (action.event.sequence !== 0 || state.last_applied_sequence !== -1) {
          return failStream(state, 'stream_sequence_invalid', action.now);
        }
        return Object.freeze({ ...state, last_applied_sequence: 0, error_code: null });
      }
      if (state.last_applied_sequence < 0) {
        return failStream(state, 'stream_sequence_invalid', action.now);
      }
      if (action.event.type === 'token') {
        if (action.event.token_index >= state.requested_max_new_tokens) {
          return failStream(state, 'stream_token_limit_exceeded', action.now);
        }
        if (action.event.token_index !== state.token_count) {
          return failStream(state, 'stream_token_order_invalid', action.now);
        }
        return Object.freeze({
          ...state,
          output: state.output + action.event.text,
          token_count: state.token_count + 1,
          last_applied_sequence: action.event.sequence,
          error_code: null,
        });
      }
      const phase = action.event.type === 'failed' ? 'failed' : action.event.type;
      const errorCode = action.event.type === 'failed' ? action.event.code : null;
      return Object.freeze({
        ...state,
        phase,
        last_applied_sequence: action.event.sequence,
        error_code: errorCode,
        history: terminalHistory(state, phase, action.now, errorCode),
      });
    }
    case 'interrupted':
      return isTerminalInferencePhase(state.phase)
        ? state
        : Object.freeze({ ...state, phase: 'interrupted', error_code: action.code });
    case 'stream_reopened':
      return isTerminalInferencePhase(state.phase)
        ? state
        : Object.freeze({
            ...state,
            phase: state.cancellation_requested ? 'cancelling' : 'streaming',
            error_code: null,
          });
    case 'cancelling':
      return isTerminalInferencePhase(state.phase)
        ? state
        : Object.freeze({ ...state, phase: 'cancelling', cancellation_requested: true });
    case 'cancel_failed':
      return isTerminalInferencePhase(state.phase)
        ? state
        : Object.freeze({
            ...state,
            phase: 'interrupted',
            error_code: action.code,
            cancellation_requested: false,
          });
    case 'stream_failed':
      return failStream(state, action.code, action.now);
    case 'submission_failed':
      return Object.freeze({ ...state, phase: 'idle', error_code: action.code });
    case 'form_failed':
      return Object.freeze({ ...state, phase: 'idle', form_error: action.reason });
    case 'clear_form_error':
      return state.form_error === null ? state : Object.freeze({ ...state, form_error: null });
    case 'clear_session':
      return Object.freeze({
        ...initialState,
        qualification_status: state.qualification_status,
        qualification: state.qualification,
      });
  }
}

function publicFailure(error: unknown, fallback: string): { code: string; retryable: boolean } {
  if (error instanceof InferenceClientError) {
    return { code: error.code, retryable: error.retryable };
  }
  return { code: fallback, retryable: false };
}

function formReason(error: unknown): string {
  if (error instanceof ProductContractError) {
    const separator = error.message.indexOf(': ');
    return separator === -1 ? 'Invalid inference request' : error.message.slice(separator + 2);
  }
  return 'Invalid inference request';
}

export interface InferenceSessionController extends InferenceSessionState {
  readonly can_submit: boolean;
  readonly submit_block_reason: string | null;
  readonly start: (
    prompt: string,
    maxNewTokens: number,
    workloadAttribution?: WorkloadAttribution,
  ) => Promise<void>;
  readonly resume: () => Promise<void>;
  readonly cancel: () => Promise<void>;
  readonly reload_qualification: () => Promise<void>;
  readonly accept_current_qualification: () => void;
  readonly clear_form_error: () => void;
  readonly clear_session: () => void;
}

export function useInferenceSession({
  client,
  now = Date.now,
  restored_state = null,
}: UseInferenceSessionOptions): InferenceSessionController {
  const [state, baseDispatch] = useReducer(
    inferenceSessionReducer,
    restoreInitialState(restored_state),
  );
  const stateRef = useRef(state);
  const mountedRef = useRef(true);
  const streamAbortRef = useRef<AbortController | null>(null);
  const loadAbortRef = useRef<AbortController | null>(null);
  const startPromiseRef = useRef<Promise<void> | null>(null);
  const streamPromiseRef = useRef<Promise<void> | null>(null);
  const cancelPromiseRef = useRef<Promise<void> | null>(null);
  const resumeAfterRestoreAttemptsRef = useRef(
    restored_state?.accepted_request !== null &&
      restored_state?.accepted_request !== undefined &&
      ['submitting', 'streaming', 'interrupted', 'cancelling'].includes(restored_state.phase)
      ? 0
      : -1,
  );

  const dispatch = useCallback((action: SessionAction): void => {
    if (!mountedRef.current) return;
    stateRef.current = inferenceSessionReducer(stateRef.current, action);
    baseDispatch(action);
  }, []);

  const loadQualification = useCallback(async (): Promise<void> => {
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    try {
      const current = await client.loadQualification(controller.signal);
      const previous = stateRef.current.qualification;
      dispatch({
        type: 'qualification_loaded',
        value: current,
        changed: previous !== null && !sameBinding(previous, current),
      });
    } catch {
      if (!controller.signal.aborted) dispatch({ type: 'qualification_unavailable' });
    } finally {
      if (loadAbortRef.current === controller) loadAbortRef.current = null;
    }
  }, [client, dispatch]);

  useEffect(() => {
    mountedRef.current = true;
    void loadQualification();
    return () => {
      mountedRef.current = false;
      loadAbortRef.current?.abort();
      streamAbortRef.current?.abort();
    };
  }, [loadQualification]);

  const runStream = useCallback(
    async (request: InferenceAcceptedResponse, cursor: number | null): Promise<void> => {
      if (streamPromiseRef.current !== null) return streamPromiseRef.current;
      const controller = new AbortController();
      streamAbortRef.current = controller;
      dispatch({ type: 'stream_reopened' });
      const operation = (async () => {
        try {
          await client.stream(
            request,
            cursor,
            (event) => {
              dispatch({ type: 'event', event, now: now() });
              if (isTerminalInferencePhase(stateRef.current.phase)) controller.abort();
            },
            controller.signal,
          );
          const latest = stateRef.current;
          if (!isTerminalInferencePhase(latest.phase)) {
            dispatch({ type: 'interrupted', code: 'stream_disconnected' });
          }
        } catch (error) {
          if (controller.signal.aborted || isTerminalInferencePhase(stateRef.current.phase)) return;
          const failure = publicFailure(error, 'stream_failed');
          if (failure.retryable || failure.code === 'stream_already_attached') {
            dispatch({ type: 'interrupted', code: failure.code });
          } else {
            dispatch({ type: 'stream_failed', code: failure.code, now: now() });
          }
        } finally {
          if (streamAbortRef.current === controller) streamAbortRef.current = null;
          streamPromiseRef.current = null;
        }
      })();
      streamPromiseRef.current = operation;
      return operation;
    },
    [client, dispatch, now],
  );

  const start = useCallback(
    async (
      prompt: string,
      maxNewTokens: number,
      workloadAttribution?: WorkloadAttribution,
    ): Promise<void> => {
      if (startPromiseRef.current !== null) return startPromiseRef.current;
      const operation = (async () => {
        const before = stateRef.current;
        const blocked = ['submitting', 'streaming', 'interrupted', 'cancelling'].includes(
          before.phase,
        )
          ? ACTIVE_REQUEST_REASON
          : before.qualification_changed
            ? QUALIFICATION_CHANGED_REASON
            : inferenceBlockReason(before.qualification, now());
        if (blocked !== null) {
          dispatch({ type: 'form_failed', reason: blocked });
          return;
        }
        let submission;
        try {
          submission = buildInferenceSubmission(prompt, maxNewTokens, before.qualification, now());
        } catch (error) {
          dispatch({ type: 'form_failed', reason: formReason(error) });
          return;
        }

        dispatch({ type: 'clear_form_error' });
        let current: ProductQualification;
        try {
          current = await client.loadQualification();
        } catch (error) {
          const failure = publicFailure(error, 'qualification_unavailable');
          dispatch({ type: 'qualification_unavailable' });
          dispatch({ type: 'submission_failed', code: failure.code });
          return;
        }
        const currentBlocked = inferenceBlockReason(current, now());
        if (currentBlocked !== null) {
          dispatch({ type: 'qualification_loaded', value: current, changed: false });
          dispatch({ type: 'form_failed', reason: currentBlocked });
          return;
        }
        const acknowledged = before.qualification;
        if (acknowledged === null || !sameBinding(acknowledged, current)) {
          dispatch({ type: 'qualification_loaded', value: current, changed: true });
          return;
        }
        try {
          submission = buildInferenceSubmission(prompt, maxNewTokens, current, now());
        } catch (error) {
          dispatch({ type: 'form_failed', reason: formReason(error) });
          return;
        }
        dispatch({
          type: 'submitting',
          binding: current.binding,
          prompt,
          max_new_tokens: maxNewTokens,
          now: now(),
          workload_attribution: workloadAttribution,
        });
        try {
          const accepted = await client.submit(submission);
          dispatch({ type: 'accepted', request: accepted });
          await runStream(accepted, null);
        } catch (error) {
          if (stateRef.current.accepted_request !== null) return;
          const failure = publicFailure(error, 'submission_failed');
          dispatch({ type: 'submission_failed', code: failure.code });
        }
      })();
      startPromiseRef.current = operation;
      try {
        await operation;
      } finally {
        startPromiseRef.current = null;
      }
    },
    [client, dispatch, now, runStream],
  );

  const resume = useCallback(async (): Promise<void> => {
    const current = stateRef.current;
    if (
      current.accepted_request === null ||
      (current.phase !== 'interrupted' && current.phase !== 'cancelling')
    ) {
      return;
    }
    const cursor = current.last_applied_sequence < 0 ? null : current.last_applied_sequence;
    await runStream(current.accepted_request, cursor);
  }, [runStream]);

  const cancel = useCallback(async (): Promise<void> => {
    const current = stateRef.current;
    if (isTerminalInferencePhase(current.phase) || current.accepted_request === null) return;
    if (cancelPromiseRef.current !== null) return cancelPromiseRef.current;
    dispatch({ type: 'cancelling' });
    const operation = (async () => {
      try {
        await client.cancel(current.accepted_request!);
      } catch (error) {
        const failure = publicFailure(error, 'cancellation_failed');
        dispatch({ type: 'cancel_failed', code: failure.code });
      } finally {
        cancelPromiseRef.current = null;
      }
    })();
    cancelPromiseRef.current = operation;
    return operation;
  }, [client, dispatch]);

  const submitBlockReason = state.qualification_status === 'loading'
    ? 'Qualification is loading'
    : state.qualification_status === 'unavailable'
      ? 'Qualification unavailable'
      : state.qualification_changed
        ? QUALIFICATION_CHANGED_REASON
        : ['submitting', 'streaming', 'interrupted', 'cancelling'].includes(state.phase)
          ? ACTIVE_REQUEST_REASON
          : inferenceBlockReason(state.qualification, now());

  useEffect(() => {
    if (
      resumeAfterRestoreAttemptsRef.current < 0 ||
      state.qualification_status !== 'ready' ||
      state.phase !== 'interrupted' ||
      state.accepted_request === null
    ) {
      return;
    }
    const attempt = resumeAfterRestoreAttemptsRef.current;
    if (attempt > 0 && state.error_code !== 'stream_already_attached') {
      resumeAfterRestoreAttemptsRef.current = -1;
      return;
    }
    if (attempt >= RESTORED_STREAM_RETRY_DELAYS_MS.length) {
      resumeAfterRestoreAttemptsRef.current = -1;
      dispatch({ type: 'stream_failed', code: 'stream_already_attached', now: now() });
      return;
    }
    const cursor = state.last_applied_sequence < 0 ? null : state.last_applied_sequence;
    const timer = window.setTimeout(() => {
      resumeAfterRestoreAttemptsRef.current = attempt + 1;
      void runStream(state.accepted_request!, cursor);
    }, RESTORED_STREAM_RETRY_DELAYS_MS[attempt]);
    return () => window.clearTimeout(timer);
  }, [dispatch, now, runStream, state.accepted_request, state.error_code, state.last_applied_sequence, state.phase, state.qualification_status]);

  return {
    ...state,
    can_submit: submitBlockReason === null,
    submit_block_reason: submitBlockReason,
    start,
    resume,
    cancel,
    reload_qualification: loadQualification,
    accept_current_qualification: () => dispatch({ type: 'qualification_accepted' }),
    clear_form_error: () => dispatch({ type: 'clear_form_error' }),
    clear_session: () => dispatch({ type: 'clear_session' }),
  };
}
