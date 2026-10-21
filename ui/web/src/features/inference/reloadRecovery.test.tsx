// SPDX-License-Identifier: AGPL-3.0-only
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  MAX_NEW_TOKENS,
  MAX_PROMPT_UTF8_BYTES,
  PRODUCT_API_PATHS,
  PRODUCT_BOOTSTRAP_PROTOCOL,
  PRODUCT_ERROR_PROTOCOL,
  PRODUCT_INFERENCE_EVENT_PROTOCOL,
  PRODUCT_INFERENCE_PROTOCOL,
  PRODUCT_QUALIFIER_AUTHORITY,
} from '../../app/contracts';
import { InferenceWorkspace } from './InferenceWorkspace';
import { ProductInferenceClient, type InferenceFetch } from './requestClient';
import { createInferenceTabSessionStore } from './sessionStore';

// A deterministic model of the request gateway's browser-visible stream
// contract (mycelium_request_gateway/service.py subscribe(), exercised by
// tests/request_gateway/test_a4_reconnect.py) behind the product BFF error
// mapping (409 + product error document):
//   * every accepted subscription advances publisher_generation by exactly one
//     and replays the retained events after the cursor under that generation;
//   * a resume whose Last-Event-ID generation is not the current one is refused
//     with stale_publisher_generation, before any other check;
//   * at most one subscription is attached; the stream closes after terminal.
// It is not the Python gateway, and it is not hardware evidence.

const NOW = 1_800_000_000_000;
const DIGEST = `sha256:${'a'.repeat(64)}`;
const REQUEST_ID = 'request-reload';

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

type ModelEvent =
  | { readonly sequence: number; readonly type: 'accepted' | 'completed' | 'failed' }
  | { readonly sequence: number; readonly type: 'token'; readonly token_index: number; readonly text: string };

interface Subscription {
  readonly generation: number;
  cursor: number;
  push: () => void;
  close: () => void;
}

class GatewayModel {
  generation = 0;
  readonly events: ModelEvent[] = [];
  active: Subscription | null = null;
  readonly subscribeResults: string[] = [];
  private tokenIndex = 0;

  readonly fetch: InferenceFetch = async (input, init) => {
    const path = String(input);
    if (path === '/api/v1/bootstrap') {
      return json({
        protocol: PRODUCT_BOOTSTRAP_PROTOCOL,
        source_mode: 'live',
        session: {
          csrf_header: 'X-Mycelium-CSRF',
          csrf_token: 'bounded-local-csrf-token',
          expires_at_unix_ms: NOW + 3_600_000,
        },
        api: PRODUCT_API_PATHS,
        limits: { max_prompt_utf8_bytes: MAX_PROMPT_UTF8_BYTES, max_new_tokens: MAX_NEW_TOKENS },
        qualification_authority: PRODUCT_QUALIFIER_AUTHORITY,
      });
    }
    if (path === '/api/v1/qualification/current') {
      return json({
        protocol: PRODUCT_INFERENCE_PROTOCOL,
        issued_at_unix_ms: NOW,
        evidence_class: 'physical_qualification',
        route_ready: true,
        reason_codes: [],
        binding: {
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
        },
      });
    }
    if (path === '/api/v1/inference' && init?.method === 'POST') {
      this.events.push({ sequence: 0, type: 'accepted' });
      return json({
        protocol: PRODUCT_INFERENCE_PROTOCOL,
        request_id: REQUEST_ID,
        accepted: true,
        event_path: `/api/v1/inference/${REQUEST_ID}/events`,
        cancel_path: `/api/v1/inference/${REQUEST_ID}/cancel`,
      });
    }
    if (path === `/api/v1/inference/${REQUEST_ID}/events`) {
      return this.subscribe(new Headers(init?.headers).get('Last-Event-ID'), init?.signal ?? undefined);
    }
    return json({ protocol: PRODUCT_ERROR_PROTOCOL, code: 'not_found', retryable: false }, 404);
  };

  private refuse(code: string): Response {
    this.subscribeResults.push(code);
    return json({ protocol: PRODUCT_ERROR_PROTOCOL, code, retryable: false }, 409);
  }

  private subscribe(lastEventId: string | null, signal: AbortSignal | undefined): Response {
    let cursor = -1;
    if (lastEventId !== null) {
      const [generation, sequence] = lastEventId.split(':').map(Number);
      if (generation !== this.generation) return this.refuse('stale_publisher_generation');
      cursor = sequence;
    }
    if (this.active !== null) return this.refuse('stream_already_attached');
    if (cursor > this.latestSequence) return this.refuse('invalid_last_event_id');
    this.generation += 1;
    this.subscribeResults.push(`subscribed:${this.generation}`);
    const encoder = new TextEncoder();
    let controllerRef: ReadableStreamDefaultController<Uint8Array> | null = null;
    const subscription: Subscription = {
      generation: this.generation,
      cursor,
      push: () => {
        const controller = controllerRef;
        if (controller === null) return;
        for (const item of this.events) {
          if (item.sequence <= subscription.cursor) continue;
          subscription.cursor = item.sequence;
          const payload = {
            protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
            request_id: REQUEST_ID,
            publisher_generation: subscription.generation,
            ...item,
          };
          controller.enqueue(encoder.encode(
            `id: ${subscription.generation}:${item.sequence}\nevent: ${item.type}\ndata: ${JSON.stringify(payload)}\n\n`,
          ));
          if (item.type === 'completed' || item.type === 'failed') {
            subscription.close();
            controller.close();
            return;
          }
        }
      },
      close: () => {
        if (this.active === subscription) this.active = null;
        controllerRef = null;
      },
    };
    this.active = subscription;
    const body = new ReadableStream<Uint8Array>({
      start: (controller) => {
        controllerRef = controller;
        signal?.addEventListener('abort', () => {
          subscription.close();
          try {
            controller.error(new DOMException('aborted', 'AbortError'));
          } catch {
            // Already closed after terminal.
          }
        }, { once: true });
        queueMicrotask(() => subscription.push());
      },
      cancel: () => subscription.close(),
    });
    return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }

  get latestSequence(): number {
    return this.events.length === 0 ? -1 : this.events[this.events.length - 1].sequence;
  }

  emitToken(text: string): void {
    this.events.push({ sequence: this.latestSequence + 1, type: 'token', token_index: this.tokenIndex, text });
    this.tokenIndex += 1;
    this.active?.push();
  }

  emitTerminal(type: 'completed' | 'failed'): void {
    this.events.push({ sequence: this.latestSequence + 1, type });
    this.active?.push();
  }
}

function outcome(): HTMLElement {
  return screen.getByRole('status', { name: 'Inference outcome' });
}

function mount(gateway: GatewayModel) {
  // A reload: a fresh client (fresh bootstrap) over the same tab sessionStorage.
  return render(
    <InferenceWorkspace
      client={new ProductInferenceClient({ fetch: gateway.fetch })}
      sessionStore={createInferenceTabSessionStore(window.sessionStorage)}
      now={() => NOW + 1}
    />,
  );
}

describe('InferenceWorkspace reload during server-side recovery', () => {
  beforeEach(() => window.sessionStorage.clear());
  afterEach(() => cleanup());

  it('rebuilds the server terminal after two reloads across a recovery cutover', async () => {
    const gateway = new GatewayModel();
    let page = mount(gateway);
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'private reload prompt' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start inference' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Start inference' }));
    await waitFor(() => expect(gateway.active).not.toBeNull());
    gateway.emitToken('A');
    await waitFor(() => expect(outcome()).toHaveTextContent('1 tokens applied'));
    // Source failure: the gateway holds the committed prefix; no new events.

    page.unmount(); // reload 1 (during recovery)
    page = mount(gateway);
    expect(outcome()).toHaveTextContent('Stream interrupted');
    // The restored page re-attaches; the recovery has not produced output yet.
    await waitFor(() => expect(gateway.subscribeResults).toContain('subscribed:2'));
    await waitFor(() => expect(outcome()).toHaveTextContent('Streaming'));

    page.unmount(); // reload 2 (right after the cutover, before successor output)
    // The successor finishes while no tab is attached: the server terminal exists.
    gateway.emitToken('B');
    gateway.emitToken('C');
    gateway.emitTerminal('completed');
    mount(gateway);

    await waitFor(() => expect(outcome()).toHaveTextContent('Completed'), { timeout: 5_000 });
    expect(outcome()).toHaveTextContent('3 tokens applied');
    expect(screen.getByRole('log', { name: 'Decoded output' })).toHaveTextContent('ABC');
    expect(gateway.subscribeResults).toEqual(['subscribed:1', 'subscribed:2', 'subscribed:3']);
    expect(screen.getAllByRole('cell', { name: 'completed' })).toHaveLength(1);
  }, 20_000);

  it('names exactly one inference outcome status among the workspace status regions', async () => {
    const gateway = new GatewayModel();
    mount(gateway);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start inference' })).toBeDefined());
    expect(screen.getAllByRole('status', { name: 'Inference outcome' })).toHaveLength(1);
    expect(outcome()).toHaveTextContent('Ready');
  });
});
