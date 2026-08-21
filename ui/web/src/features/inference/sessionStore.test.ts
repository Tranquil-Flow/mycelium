import { describe, expect, it } from 'vitest';
import {
  PRODUCT_INFERENCE_PROTOCOL,
  type InferenceAcceptedResponse,
  type QualificationBinding,
} from '../../app/contracts';
import type { InferenceSessionState } from './types';
import {
  INFERENCE_TAB_SESSION_STORAGE_KEY,
  createInferenceTabSessionStore,
} from './sessionStore';

const DIGEST = `sha256:${'a'.repeat(64)}`;

class MemoryStorage {
  readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

const binding: QualificationBinding = {
  qualification_id: 'qualification-a',
  qualification_digest: DIGEST,
  deployment_id: 'deployment-a',
  deployment_epoch: 1,
  topology_version: 1,
  model_id: 'model-a',
  resolved_commit: 'commit-a',
  manifest_digest: DIGEST,
  path_manifest_digest: DIGEST,
  stage_load_proof_digests: [DIGEST],
};

const accepted: InferenceAcceptedResponse = {
  protocol: PRODUCT_INFERENCE_PROTOCOL,
  request_id: 'request-a',
  accepted: true,
  event_path: '/api/v1/inference/request-a/events',
  cancel_path: '/api/v1/inference/request-a/cancel',
};

const session: InferenceSessionState = {
  qualification_status: 'ready',
  qualification: null,
  qualification_changed: false,
  phase: 'completed',
  accepted_request: accepted,
  captured_binding: binding,
  requested_max_new_tokens: 8,
  submitted_prompt: 'private prompt',
  output: 'useful output',
  token_count: 2,
  last_applied_sequence: 3,
  publisher_generation: 2,
  error_code: null,
  form_error: null,
  cancellation_requested: false,
  started_at_unix_ms: 100,
  history: [{
    request_id: accepted.request_id,
    prompt: 'private prompt',
    response: 'useful output',
    terminal_state: 'completed',
    token_count: 2,
    started_at_unix_ms: 100,
    finished_at_unix_ms: 200,
    deployment_id: binding.deployment_id,
    model_id: binding.model_id,
    error_code: null,
  }],
};

describe('InferenceTabSessionStore', () => {
  it('round-trips a bounded terminal inference snapshot without qualification authority', () => {
    const storage = new MemoryStorage();
    const store = createInferenceTabSessionStore(storage);

    store.save({ prompt: 'private prompt', max_new_tokens: 8, session });

    const restored = store.load();
    expect(restored?.prompt).toBe('private prompt');
    expect(restored?.session.output).toBe('useful output');
    expect(restored?.session.history).toHaveLength(1);
    expect(restored?.session.history[0]?.prompt).toBe('private prompt');
    expect(restored?.session.history[0]?.response).toBe('useful output');
    expect(restored?.session.qualification_status).toBe('loading');
    expect(restored?.session.qualification).toBeNull();
  });

  it('round-trips cancel_unconfirmed without deleting private prompt state', () => {
    const storage = new MemoryStorage();
    const store = createInferenceTabSessionStore(storage);
    const cancelUnconfirmed: InferenceSessionState = {
      ...session,
      phase: 'cancel_unconfirmed',
      history: [],
    };

    store.save({
      prompt: 'A4-BROWSER-PRIVATE-RELOAD-PROBE Explain request scoped cleanup.',
      max_new_tokens: 64,
      session: cancelUnconfirmed,
    });

    const restored = store.load();
    expect(restored?.prompt).toContain('A4-BROWSER-PRIVATE-RELOAD-PROBE');
    expect(restored?.session.phase).toBe('cancel_unconfirmed');
    expect(restored?.session.accepted_request?.request_id).toBe(accepted.request_id);
    expect(storage.getItem(INFERENCE_TAB_SESSION_STORAGE_KEY)).not.toBeNull();
  });

  it('rejects and removes malformed or over-bound tab state', () => {
    const storage = new MemoryStorage();
    const store = createInferenceTabSessionStore(storage);
    storage.setItem(INFERENCE_TAB_SESSION_STORAGE_KEY, '{bad json');

    expect(store.load()).toBeNull();
    expect(storage.getItem(INFERENCE_TAB_SESSION_STORAGE_KEY)).toBeNull();

    storage.setItem(
      INFERENCE_TAB_SESSION_STORAGE_KEY,
      JSON.stringify({ version: 1, prompt: 'x', max_new_tokens: 999_999, session: {} }),
    );
    expect(store.load()).toBeNull();
    expect(storage.getItem(INFERENCE_TAB_SESSION_STORAGE_KEY)).toBeNull();
  });

  it('fails open when browser storage is unavailable', () => {
    const store = createInferenceTabSessionStore({
      getItem: () => { throw new Error('blocked'); },
      setItem: () => { throw new Error('blocked'); },
      removeItem: () => { throw new Error('blocked'); },
    });

    expect(store.load()).toBeNull();
    expect(() => store.save({ prompt: '', max_new_tokens: 8, session })).not.toThrow();
    expect(() => store.clear()).not.toThrow();
  });
});
