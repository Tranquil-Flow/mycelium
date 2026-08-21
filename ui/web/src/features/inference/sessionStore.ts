import {
  MAX_NEW_TOKENS,
  MAX_PROMPT_UTF8_BYTES,
  decodeInferenceAccepted,
  type QualificationBinding,
} from '../../app/contracts';
import type {
  InferenceHistoryEntry,
  InferencePhase,
  InferenceSessionState,
  WorkloadAttribution,
} from './types';

const STORAGE_KEY = 'mycelium.inference.tab-session.v1';
const STORAGE_VERSION = 1;
const MAX_OUTPUT_CHARS = 1_048_576;
const MAX_SERIALIZED_CHARS = 2_000_000;
const MAX_HISTORY_ITEMS = 20;
const SHA256_REF = /^sha256:[0-9a-f]{64}$/;
const SAFE_ERROR_CODE = /^[a-z][a-z0-9_]{0,63}$/;
const PHASES = new Set<InferencePhase>([
  'idle',
  'submitting',
  'streaming',
  'interrupted',
  'cancelling',
  'cancel_unconfirmed',
  'completed',
  'cancelled',
  'failed',
]);
const TERMINAL_STATES = new Set(['completed', 'cancelled', 'failed']);

export interface InferenceTabSnapshot {
  readonly prompt: string;
  readonly max_new_tokens: number;
  readonly session: InferenceSessionState;
}

export interface InferenceTabSessionStore {
  readonly load: () => InferenceTabSnapshot | null;
  readonly save: (snapshot: InferenceTabSnapshot) => void;
  readonly clear: () => void;
}

type SessionStoragePort = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function safeString(value: unknown, maximum = 256): string | null {
  return typeof value === 'string' && value.length <= maximum ? value : null;
}

function safeInteger(value: unknown, minimum: number, maximum: number): number | null {
  return Number.isSafeInteger(value) && (value as number) >= minimum && (value as number) <= maximum
    ? value as number
    : null;
}

function decodeBinding(value: unknown): QualificationBinding | null {
  const item = record(value);
  if (item === null) return null;
  const qualificationId = safeString(item.qualification_id);
  const qualificationDigest = safeString(item.qualification_digest);
  const deploymentId = safeString(item.deployment_id);
  const deploymentEpoch = safeInteger(item.deployment_epoch, 0, Number.MAX_SAFE_INTEGER);
  const topologyVersion = safeInteger(item.topology_version, 0, Number.MAX_SAFE_INTEGER);
  const modelId = safeString(item.model_id);
  const resolvedCommit = safeString(item.resolved_commit);
  const manifestDigest = safeString(item.manifest_digest);
  const pathManifestDigest = safeString(item.path_manifest_digest);
  const proofs = item.stage_load_proof_digests;
  if (
    qualificationId === null || qualificationId.length === 0 ||
    qualificationDigest === null || !SHA256_REF.test(qualificationDigest) ||
    deploymentId === null || deploymentId.length === 0 ||
    deploymentEpoch === null || topologyVersion === null ||
    modelId === null || modelId.length === 0 ||
    resolvedCommit === null || resolvedCommit.length === 0 ||
    manifestDigest === null || !SHA256_REF.test(manifestDigest) ||
    pathManifestDigest === null || !SHA256_REF.test(pathManifestDigest) ||
    !Array.isArray(proofs) || proofs.length === 0 || proofs.length > 128 ||
    proofs.some((proof) => typeof proof !== 'string' || !SHA256_REF.test(proof))
  ) {
    return null;
  }
  return Object.freeze({
    qualification_id: qualificationId,
    qualification_digest: qualificationDigest,
    deployment_id: deploymentId,
    deployment_epoch: deploymentEpoch,
    topology_version: topologyVersion,
    model_id: modelId,
    resolved_commit: resolvedCommit,
    manifest_digest: manifestDigest,
    path_manifest_digest: pathManifestDigest,
    stage_load_proof_digests: Object.freeze([...proofs] as string[]),
  });
}

function decodeWorkloadAttribution(value: unknown): WorkloadAttribution | null {
  const item = record(value);
  if (item === null) return null;
  const profileId = safeString(item.profile_id, 128);
  const qosClass = safeString(item.qos_class, 16);
  const policyId = safeString(item.planner_policy_id, 32);
  if (
    profileId === null || profileId.length === 0 ||
    !['interactive', 'batch'].includes(qosClass ?? '') ||
    !['balanced', 'decode_tpot', 'prefill_ttft'].includes(policyId ?? '') ||
    item.attribution_scope !== 'client_visible_planner_intent'
  ) return null;
  return Object.freeze({
    profile_id: profileId,
    qos_class: qosClass as WorkloadAttribution['qos_class'],
    planner_policy_id: policyId as WorkloadAttribution['planner_policy_id'],
    attribution_scope: 'client_visible_planner_intent',
  });
}

function decodeHistory(value: unknown): readonly InferenceHistoryEntry[] | null {
  if (!Array.isArray(value) || value.length > MAX_HISTORY_ITEMS) return null;
  const decoded: InferenceHistoryEntry[] = [];
  for (const candidate of value) {
    const item = record(candidate);
    if (item === null) return null;
    const requestId = safeString(item.request_id, 128);
    const terminalState = safeString(item.terminal_state, 16);
    const tokenCount = safeInteger(item.token_count, 0, MAX_NEW_TOKENS);
    const startedAt = safeInteger(item.started_at_unix_ms, 0, Number.MAX_SAFE_INTEGER);
    const finishedAt = safeInteger(item.finished_at_unix_ms, 0, Number.MAX_SAFE_INTEGER);
    const deploymentId = safeString(item.deployment_id);
    const modelId = safeString(item.model_id);
    const prompt = item.prompt === undefined
      ? ''
      : safeString(item.prompt, MAX_PROMPT_UTF8_BYTES);
    const response = item.response === undefined
      ? ''
      : safeString(item.response, MAX_OUTPUT_CHARS);
    const errorCode = item.error_code === null ? null : safeString(item.error_code, 64);
    const workloadAttribution = item.workload_attribution === undefined
      ? undefined
      : decodeWorkloadAttribution(item.workload_attribution);
    if (
      requestId === null || requestId.length === 0 ||
      terminalState === null || !TERMINAL_STATES.has(terminalState) ||
      tokenCount === null || startedAt === null || finishedAt === null || finishedAt < startedAt ||
      deploymentId === null || deploymentId.length === 0 ||
      modelId === null || modelId.length === 0 || prompt === null || response === null ||
      (errorCode !== null && !SAFE_ERROR_CODE.test(errorCode))
      || (item.workload_attribution !== undefined && workloadAttribution === null)
    ) {
      return null;
    }
    decoded.push(Object.freeze({
      request_id: requestId,
      prompt,
      response,
      terminal_state: terminalState as InferenceHistoryEntry['terminal_state'],
      token_count: tokenCount,
      started_at_unix_ms: startedAt,
      finished_at_unix_ms: finishedAt,
      deployment_id: deploymentId,
      model_id: modelId,
      error_code: errorCode,
      ...(workloadAttribution == null ? {} : { workload_attribution: workloadAttribution }),
    }));
  }
  if (new Set(decoded.map((entry) => entry.request_id)).size !== decoded.length) return null;
  return Object.freeze(decoded);
}

function decodeSession(value: unknown): InferenceSessionState | null {
  const item = record(value);
  if (item === null) return null;
  const phase = safeString(item.phase, 32) as InferencePhase | null;
  const accepted = item.accepted_request === null
    ? null
    : (() => {
        try {
          return decodeInferenceAccepted(item.accepted_request);
        } catch {
          return null;
        }
      })();
  const binding = item.captured_binding === null ? null : decodeBinding(item.captured_binding);
  const requestedMax = safeInteger(item.requested_max_new_tokens, 0, MAX_NEW_TOKENS);
  const submittedPrompt = item.submitted_prompt === undefined || item.submitted_prompt === null
    ? null
    : safeString(item.submitted_prompt, MAX_PROMPT_UTF8_BYTES);
  const output = safeString(item.output, MAX_OUTPUT_CHARS);
  const tokenCount = safeInteger(item.token_count, 0, MAX_NEW_TOKENS);
  const sequence = safeInteger(item.last_applied_sequence, -1, Number.MAX_SAFE_INTEGER);
  const publisherGeneration = safeInteger(item.publisher_generation, 0, Number.MAX_SAFE_INTEGER);
  const errorCode = item.error_code === null ? null : safeString(item.error_code, 64);
  const startedAt = item.started_at_unix_ms === null
    ? null
    : safeInteger(item.started_at_unix_ms, 0, Number.MAX_SAFE_INTEGER);
  const history = decodeHistory(item.history);
  const capturedWorkloadAttribution = item.captured_workload_attribution === undefined || item.captured_workload_attribution === null
    ? null
    : decodeWorkloadAttribution(item.captured_workload_attribution);
  if (
    phase === null || !PHASES.has(phase) ||
    (item.accepted_request !== null && accepted === null) ||
    (item.captured_binding !== null && binding === null) ||
    requestedMax === null || submittedPrompt === null && item.submitted_prompt !== undefined && item.submitted_prompt !== null ||
    output === null || tokenCount === null || sequence === null || publisherGeneration === null ||
    (errorCode !== null && !SAFE_ERROR_CODE.test(errorCode)) ||
    (item.started_at_unix_ms !== null && startedAt === null) || history === null ||
    tokenCount > requestedMax ||
    (accepted === null) !== (binding === null)
    || (item.captured_workload_attribution !== undefined && item.captured_workload_attribution !== null && capturedWorkloadAttribution === null)
  ) {
    return null;
  }
  return Object.freeze({
    qualification_status: 'loading',
    qualification: null,
    qualification_changed: false,
    phase,
    accepted_request: accepted,
    captured_binding: binding,
    requested_max_new_tokens: requestedMax,
    submitted_prompt: submittedPrompt,
    output,
    token_count: tokenCount,
    last_applied_sequence: sequence,
    publisher_generation: publisherGeneration,
    error_code: errorCode,
    form_error: null,
    cancellation_requested: false,
    started_at_unix_ms: startedAt,
    history,
    captured_workload_attribution: capturedWorkloadAttribution,
  });
}

function decodeSnapshot(value: unknown): InferenceTabSnapshot | null {
  const item = record(value);
  if (item === null || item.version !== STORAGE_VERSION) return null;
  const prompt = safeString(item.prompt, MAX_PROMPT_UTF8_BYTES);
  const maxNewTokens = safeInteger(item.max_new_tokens, 1, MAX_NEW_TOKENS);
  const session = decodeSession(item.session);
  if (prompt === null || maxNewTokens === null || session === null) return null;
  if (new TextEncoder().encode(prompt).byteLength > MAX_PROMPT_UTF8_BYTES) return null;
  return Object.freeze({ prompt, max_new_tokens: maxNewTokens, session });
}

function serializedSnapshot(snapshot: InferenceTabSnapshot): string {
  return JSON.stringify({
    version: STORAGE_VERSION,
    prompt: snapshot.prompt,
    max_new_tokens: snapshot.max_new_tokens,
    session: {
      phase: snapshot.session.phase,
      accepted_request: snapshot.session.accepted_request,
      captured_binding: snapshot.session.captured_binding,
      requested_max_new_tokens: snapshot.session.requested_max_new_tokens,
      submitted_prompt: snapshot.session.submitted_prompt,
      output: snapshot.session.output,
      token_count: snapshot.session.token_count,
      last_applied_sequence: snapshot.session.last_applied_sequence,
      publisher_generation: snapshot.session.publisher_generation,
      error_code: snapshot.session.error_code,
      started_at_unix_ms: snapshot.session.started_at_unix_ms,
      history: snapshot.session.history,
      captured_workload_attribution: snapshot.session.captured_workload_attribution ?? null,
    },
  });
}

export function createInferenceTabSessionStore(
  storage: SessionStoragePort,
): InferenceTabSessionStore {
  return Object.freeze({
    load: () => {
      try {
        const serialized = storage.getItem(STORAGE_KEY);
        if (serialized === null || serialized.length > MAX_SERIALIZED_CHARS) return null;
        const decoded = decodeSnapshot(JSON.parse(serialized) as unknown);
        if (decoded === null) storage.removeItem(STORAGE_KEY);
        return decoded;
      } catch {
        try {
          storage.removeItem(STORAGE_KEY);
        } catch {
          // Storage can be unavailable under restrictive browser policies.
        }
        return null;
      }
    },
    save: (snapshot: InferenceTabSnapshot) => {
      try {
        const serialized = serializedSnapshot(snapshot);
        if (serialized.length <= MAX_SERIALIZED_CHARS) storage.setItem(STORAGE_KEY, serialized);
      } catch {
        // Inference remains usable when tab-scoped storage is unavailable.
      }
    },
    clear: () => {
      try {
        storage.removeItem(STORAGE_KEY);
      } catch {
        // Inference remains usable when tab-scoped storage is unavailable.
      }
    },
  });
}

export function createBrowserInferenceTabSessionStore(): InferenceTabSessionStore | null {
  if (typeof window === 'undefined') return null;
  try {
    return createInferenceTabSessionStore(window.sessionStorage);
  } catch {
    return null;
  }
}

export const INFERENCE_TAB_SESSION_STORAGE_KEY = STORAGE_KEY;
