// SPDX-License-Identifier: AGPL-3.0-only
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  PRODUCT_INFERENCE_EVENT_PROTOCOL,
  PRODUCT_INFERENCE_PROTOCOL,
  type InferenceEvent,
  type InferenceSubmission,
  type ProductQualification,
} from '../../app/contracts';
import type { InferenceClient } from '../inference/requestClient';
import type { ProductEntity, ProductSnapshot } from '../productEvidence/contracts';
import { ConsumerDemoPage } from './ConsumerDemoPage';
import {
  ConsumerRouteStatusError,
  type ConsumerRouteStatusFacts,
} from './consumerObservatory';
import type { ConsumerRouteStatusPort } from './ConsumerRoutePanel';

// Test-only protocol inputs. These are not live qualification or model evidence.
const hash = (character: string) => `sha256:${character.repeat(64)}`;
const qualification: ProductQualification = {
  protocol: PRODUCT_INFERENCE_PROTOCOL, issued_at_unix_ms: 1_000,
  evidence_class: 'physical_qualification', route_ready: true, reason_codes: [],
  binding: { qualification_id: 'test-qualification', qualification_digest: hash('a'),
    deployment_id: 'test-deployment', deployment_epoch: 1, topology_version: 1,
    model_id: 'test-only-model', resolved_commit: 'test-commit', manifest_digest: hash('b'),
    path_manifest_digest: hash('c'), stage_load_proof_digests: [hash('d')] },
};
const accepted = { protocol: PRODUCT_INFERENCE_PROTOCOL, request_id: 'test-request', accepted: true as const,
  event_path: '/api/v1/inference/test-request/events', cancel_path: '/api/v1/inference/test-request/cancel' };

class TestClient implements InferenceClient {
  current = qualification;
  submitted: InferenceSubmission[] = [];
  emit: (event: InferenceEvent) => void = () => undefined;
  disconnect: () => void = () => undefined;
  cursor: number | null = null;
  loadQualification = async () => this.current;
  submit = async (submission: InferenceSubmission) => { this.submitted.push(submission); return accepted; };
  stream: InferenceClient['stream'] = async (_request, cursor, emit, signal) => {
    this.emit = emit; this.cursor = cursor;
    await new Promise<void>((resolve, reject) => {
      this.disconnect = resolve;
      signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    });
  };
  cancel = async () => ({ protocol: PRODUCT_INFERENCE_PROTOCOL, request_id: accepted.request_id, cancelled: true });
  event(sequence: number, event: Partial<InferenceEvent>) {
    this.emit({ protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL, request_id: accepted.request_id,
      publisher_generation: 1, sequence, ...event } as InferenceEvent);
  }
}

const routeStatusFacts = (overrides: Partial<ConsumerRouteStatusFacts> = {}): ConsumerRouteStatusFacts => ({
  generation: 7, source_cursor: 9, observed_at_unix_ms: 1_000, route_ready: true,
  buffered_sessions: 0, dropped_quarantine_count: 0, quarantine_capacity: 16,
  sessions: { total: 0, by_state: { accepted: 0, streaming: 0, completed: 0, cancelled: 0, failed: 0, quarantined: 0 },
    failed_request_ids: [], cancelled_request_ids: [], quarantined: [] },
  incidents: [], qualification: null, ...overrides,
});

const routeStatusOk: ConsumerRouteStatusPort = async () => routeStatusFacts();
const routeStatusFailed: ConsumerRouteStatusPort = async () => {
  throw new ConsumerRouteStatusError('route_status_unavailable', true, 503);
};

const evidence = vi.hoisted(() => ({
  latest: null as any, visible: null as any, configured: false, loading: false, frozen: false, error_code: null,
}));
vi.mock('../productEvidence/ProductEvidenceContext', () => ({ useProductEvidence: () => evidence }));

function snapshotWith(
  entities: readonly ProductEntity[],
  relations: readonly { readonly relation_id: string; readonly kind: 'placed_on' | 'assigned_to'; readonly from_entity_id: string; readonly to_entity_id: string; readonly source_id: string }[],
): ProductSnapshot {
  return {
    protocol: 'mycelium.product_snapshot.v1',
    publication: { snapshot_id: 'snapshot-1', generation: 12, cursor: 34, published_at_unix_ms: 1_000, source_mode: 'live' },
    supported_entity_kinds: [], source_states: [], entities, relations, readiness: [], notices: [],
    internet_native: {}, provenance: { projector: 'product-spine', projector_version: '1', source_mode: 'live' },
  } as unknown as ProductSnapshot;
}

function entity(entity_id: string, kind: ProductEntity['kind'], attributes: Record<string, unknown>): ProductEntity {
  return { entity_id, kind, label: `label-${entity_id}`, source_id: 'route-source',
    binding: { deployment_id: 'test-deployment', deployment_epoch: 1, route_id: 'route-abc123', route_generation: 4, topology_version: 4 },
    freshness: { status: 'current', observed_at_unix_ms: 1_000, valid_until_unix_ms: 1_000 }, attributes };
}

function setup(options: {
  client?: TestClient; routeStatus?: ConsumerRouteStatusPort; clock?: { value: number };
} = {}) {
  const client = options.client ?? new TestClient();
  const clock = options.clock ?? { value: 1_000 };
  const now = () => clock.value;
  const rendered = render(
    <ConsumerDemoPage
      client={client}
      now={now}
      routeStatus={options.routeStatus ?? routeStatusOk}
    />,
  );
  return { client, clock, now, ...rendered };
}

async function start() {
  fireEvent.change(screen.getByLabelText('Your prompt'), { target: { value: 'A test-only prompt' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send prompt' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Send prompt' }));
  await screen.findByText('Waiting for first token');
}

beforeEach(() => {
  Object.assign(evidence, { latest: null, visible: null, configured: false, loading: false, frozen: false, error_code: null });
});
afterEach(() => { vi.useRealTimers(); });

describe('consumer Try-it demo page', () => {
  it('streams the answer as it arrives and reports client TTFT and TPOT as measured', async () => {
    const { client, clock } = setup();
    await start();
    act(() => client.event(0, { type: 'accepted' }));
    expect(screen.getByLabelText('TTFT')).toHaveTextContent('Not measured');

    clock.value = 2_250;
    act(() => client.event(1, { type: 'token', token_index: 0, text: 'Hello' }));
    expect(screen.getByRole('log')).toHaveTextContent('Hello');
    expect(screen.getByLabelText('TTFT')).toHaveTextContent('1250.0 ms');
    expect(screen.getByLabelText('TPOT')).toHaveTextContent('Not measured');

    clock.value = 2_350;
    act(() => client.event(2, { type: 'token', token_index: 1, text: ' there' }));
    expect(screen.getByRole('log')).toHaveTextContent('Hello there');
    expect(screen.getByLabelText('TPOT')).toHaveTextContent('100.0 ms');
    expect(screen.getByLabelText('Last token')).toHaveTextContent('1350.0 ms');
    expect(screen.getByLabelText('Tokens counted')).toHaveTextContent('2');
    expect(screen.getByText(/Client-measured in this browser/)).toBeVisible();
    expect(screen.getByLabelText('Output completeness')).toHaveTextContent('Streaming');

    await act(async () => client.event(3, { type: 'completed' }));
    expect(screen.getByLabelText('Output completeness')).toHaveTextContent('Complete output');
    expect(screen.getByLabelText('TTFT')).toHaveTextContent('1250.0 ms');
    expect(screen.getByLabelText('TPOT')).toHaveTextContent('100.0 ms');
  });

  it('renders a failed terminal honestly: named code, partial answer marked incomplete, retry offered', async () => {
    const { client } = setup();
    await start();
    act(() => {
      client.event(0, { type: 'accepted' });
      client.event(1, { type: 'token', token_index: 0, text: 'Half an answer' });
    });
    await act(async () => client.event(2, { type: 'failed', code: 'worker_unavailable' }));

    expect(screen.getByRole('alert')).toHaveTextContent('worker_unavailable');
    expect(screen.getByText('Request failed')).toBeVisible();
    expect(screen.getByRole('log')).toHaveTextContent('Half an answer');
    expect(screen.getByLabelText('Output completeness')).toHaveTextContent('Partial output · not complete');
    expect(screen.queryByText('Complete output')).not.toBeInTheDocument();
    expect(screen.getByText(/not a complete answer/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Retry prompt' })).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: 'Retry prompt' }));
    await waitFor(() => expect(client.submitted).toHaveLength(2));
  });

  it('shows a measured reconnect wait for a lost stream and reopens the same request, not a new one', async () => {
    vi.useFakeTimers();
    const clock = { value: 1_000 };
    const { client } = setup({ clock });
    const flush = async (elapsed_ms = 1) => {
      await act(async () => { await vi.advanceTimersByTimeAsync(elapsed_ms); });
    };

    fireEvent.change(screen.getByLabelText('Your prompt'), { target: { value: 'A test-only prompt' } });
    await flush();
    expect(screen.getByRole('button', { name: 'Send prompt' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Send prompt' }));
    await flush();
    expect(screen.getByText('Waiting for first token')).toBeVisible();

    act(() => { client.event(0, { type: 'accepted' }); client.event(1, { type: 'token', token_index: 0, text: 'Partial' }); });
    await act(async () => client.disconnect());
    await flush();
    expect(screen.getByText('Stream interrupted')).toBeVisible();
    expect(screen.getByText(/Reconnecting \(~0 s\)/)).toBeVisible();
    expect(screen.getByText(/without a terminal event/)).toBeVisible();

    clock.value = 5_000;
    await flush(1_000);
    expect(screen.getByText(/Reconnecting \(~4 s\)/)).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Reconnect stream' }));
    await flush();
    expect(client.cursor).toBe(1);
    expect(client.submitted).toHaveLength(1);
    act(() => client.event(2, { type: 'token', token_index: 1, text: ' resumed' }));
    await flush();
    expect(screen.getByText('Streaming response')).toBeVisible();
    expect(screen.queryByText(/Reconnecting \(~/)).not.toBeInTheDocument();
    await act(async () => client.event(3, { type: 'completed' }));
    expect(screen.getByLabelText('Output completeness')).toHaveTextContent('Complete output');
    expect(screen.getByRole('log')).toHaveTextContent('Partial resumed');
  });

  it('names an observed route loss while a request is in flight and keeps the controls', async () => {
    const { client } = setup({ routeStatus: async () => routeStatusFacts({ route_ready: false }) });
    await screen.findByText('Live route status read');
    await start();
    act(() => client.event(0, { type: 'accepted' }));
    expect(screen.getByText(/route_unavailable/)).toBeVisible();
    expect(screen.getByText(/the origin reports route_ready=false/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Stop generation' })).toBeEnabled();
    expect(screen.getByLabelText('Output completeness')).toHaveTextContent('Streaming');
  });

  it('gates operator and evidence surfaces behind /operator from the consumer root', async () => {
    setup();
    for (const link of screen.getAllByRole('link')) {
      const href = link.getAttribute('href') ?? '';
      if (href.startsWith('/')) expect(href).toMatch(/^\/operator/);
    }
    for (const label of ['Inference', 'Device Lab', 'Nodes', 'Plans', 'Readiness', 'Incidents', 'Settings']) {
      expect(screen.queryByRole('link', { name: new RegExp(`^${label}$`) })).not.toBeInTheDocument();
    }
    expect(screen.getByText(/No single-host fallback/)).toBeVisible();
  });

  it('renders a named route-status failure and substitutes nothing for the missing document', async () => {
    setup({ routeStatus: routeStatusFailed });
    await screen.findByText(/Route status unavailable · route_status_unavailable/);
    expect(screen.getByText(/Nothing is substituted for it/)).toBeVisible();
    expect(screen.getByLabelText('Route status generation')).toHaveTextContent('Unavailable');
    expect(screen.getByLabelText('Route identity')).toHaveTextContent('Unavailable');
    expect(screen.getByLabelText('Product snapshot generation')).toHaveTextContent('Unavailable');
    expect(screen.getByLabelText('Route ready')).toHaveTextContent('true');
    expect(screen.queryByLabelText('Route ready (status document)')).not.toBeInTheDocument();
    expect(screen.queryByText('Live route status read')).not.toBeInTheDocument();
    expect(screen.getByText(/publish no stage or layer split/)).toBeVisible();
  });

  it('renders published stage and node placement from the product snapshot without inventing one', async () => {
    evidence.latest = { status: 'connected', freshness: 'current', source_mode: 'live' };
    evidence.visible = {
      status: 'connected', freshness: 'current', source_mode: 'live',
      snapshot: snapshotWith(
        [
          entity('route-abc123', 'route', { model_id: 'test-only-model', decode_mode: 'complete_context_replay', route_alive: true, placement_provenance: 'operator_selected' }),
          entity('stage-0', 'stage', { stage_index: 0, start_layer: 0, end_layer_exclusive: 12, component_roles: ['embedding'], decode_mode: 'complete_context_replay' }),
          entity('device-abc', 'device', { peer_class: 'mac_mlx_iroh', transport: 'iroh', runtime_backend: 'mlx', lifecycle: 'ready', lease_freshness: 'fresh', placement_id: null }),
        ],
        [{ relation_id: 'r-1', kind: 'placed_on', from_entity_id: 'stage-0', to_entity_id: 'device-abc', source_id: 'route-source' }],
      ),
    };

    const { container } = setup();
    await screen.findByText('Live route status read');
    expect(screen.getByLabelText('Route generation')).toHaveTextContent('4');
    expect(screen.getByLabelText('Route identity')).toHaveTextContent('route-abc123');
    expect(screen.getByLabelText('Product snapshot generation')).toHaveTextContent('12');
    expect(screen.getByLabelText('Route status generation')).toHaveTextContent('7');
    expect(screen.getByLabelText('Stage load proofs')).toHaveTextContent('1');
    expect(screen.getByText('0 – 12 (end exclusive)')).toBeVisible();
    expect(container.querySelector('[data-consumer-stage="stage-0"]')).not.toBeNull();
    expect(container.querySelector('[data-consumer-node="device-abc"]')).not.toBeNull();
    expect(screen.getByText(/label-device-abc · mac_mlx_iroh · iroh/)).toBeVisible();
    expect(screen.queryByText(/publish no stage or layer split/)).not.toBeInTheDocument();
  });
});
