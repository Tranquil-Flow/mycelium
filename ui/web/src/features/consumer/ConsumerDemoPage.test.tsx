// SPDX-License-Identifier: AGPL-3.0-only
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PRODUCT_INFERENCE_PROTOCOL, PRODUCT_INFERENCE_EVENT_PROTOCOL, type ProductQualification, type InferenceEvent, type InferenceSubmission } from '../../app/contracts';
import type { InferenceClient } from '../inference/requestClient';
import { ConsumerDemoPage } from './ConsumerDemoPage';
import type { ConsumerRouteStatusFacts } from './consumerObservatory';
import type { ConsumerRouteStatusPort } from './ConsumerRoutePanel';

// Test-only protocol fixtures. These are not live qualification or model evidence.
const NOW = 1_800_000_000_000;
const hash = `sha256:${'a'.repeat(64)}`;
const qualification: ProductQualification = {
  protocol: PRODUCT_INFERENCE_PROTOCOL, issued_at_unix_ms: NOW,
  evidence_class: 'physical_qualification', route_ready: true, reason_codes: [],
  binding: { qualification_id: 'test-qualification', qualification_digest: hash,
    deployment_id: 'test-deployment', deployment_epoch: 1, topology_version: 1,
    model_id: 'test-only-model', resolved_commit: 'test-commit', manifest_digest: hash,
    path_manifest_digest: hash, stage_load_proof_digests: [hash] },
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
const evidence = vi.hoisted(() => ({ latest: null as any, visible: null as any, configured: false, loading: false, frozen: false, error_code: null }));
vi.mock('../productEvidence/ProductEvidenceContext', () => ({ useProductEvidence: () => evidence }));
// No route-status document in this file: the panel stays in its loading state so
// these tests exercise the request engine and the consumer surface alone.
const pendingRouteStatus: ConsumerRouteStatusPort = () =>
  new Promise<ConsumerRouteStatusFacts>(() => undefined);
const setup = (client = new TestClient()) => {
  const rendered = render(
    <ConsumerDemoPage client={client} now={() => NOW} routeStatus={pendingRouteStatus} />,
  );
  return { client, ...rendered };
};
async function start() {
  fireEvent.change(screen.getByLabelText('Your prompt'), { target: { value: 'A test-only prompt' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send prompt' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Send prompt' }));
  await screen.findByText('Waiting for first token');
}
beforeEach(() => { Object.assign(evidence, { latest: null, visible: null, configured: false, loading: false, frozen: false }); });
describe('ConsumerDemoPage', () => {
  it('keeps missing route facts unavailable, reports no latency until it is measured, and points operator links at /operator', async () => {
    setup();
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('A little question. A shared intelligence.');
    expect(screen.getByText(/Cold-restart status and ETA are unavailable/)).toBeVisible();
    expect(screen.getByLabelText('TTFT')).toHaveTextContent('Not measured');
    expect(screen.getByLabelText('TPOT')).toHaveTextContent('Not measured');
    expect(screen.getByLabelText('Output completeness')).toHaveTextContent('No output yet');
    expect(screen.getByText(/No layer-to-node split is inferred/)).toBeVisible();
    expect(screen.getByText(/Mac names, hostnames and OS identities are not part of the published contract/)).toBeVisible();
    for (const link of screen.getAllByRole('link').filter(a => /Operator|Evidence/.test(a.textContent ?? ''))) {
      expect(link.getAttribute('href')).toMatch(/^\/operator/);
    }
    // The model identity is published twice: the request header and the route panel.
    await waitFor(() => expect(screen.getAllByText('test-only-model').length).toBeGreaterThan(0));
  });
  it('streams only real client events and retries a named terminal failure through the existing session engine', async () => {
    const { client } = setup(); await start();
    act(() => { client.event(0, { type: 'accepted' }); client.event(1, { type: 'token', token_index: 0, text: 'TEST OUTPUT' }); });
    expect(screen.getByRole('log')).toHaveTextContent('TEST OUTPUT');
    expect(screen.getByText('Streaming response')).toBeVisible();
    await act(async () => client.event(2, { type: 'failed', code: 'worker_unavailable' }));
    expect(screen.getByRole('alert')).toHaveTextContent('worker_unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry prompt' }));
    await waitFor(() => expect(client.submitted).toHaveLength(2));
    expect(client.submitted[1].prompt).toBe('A test-only prompt');
  });
  it('resumes an interrupted stream from its cursor without submitting a second request', async () => {
    const { client } = setup(); await start();
    act(() => { client.event(0, { type: 'accepted' }); client.event(1, { type: 'token', token_index: 0, text: 'Partial' }); });
    await act(async () => client.disconnect());
    expect(screen.getByText('Stream interrupted')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect stream' }));
    await screen.findByText('Reconnecting stream');
    expect(client.cursor).toBe(1); expect(client.submitted).toHaveLength(1);
    act(() => client.event(2, { type: 'token', token_index: 1, text: ' resumed' }));
    expect(screen.getByText('Streaming response')).toBeVisible();
    await act(async () => client.event(3, { type: 'completed' }));
    expect(screen.getByText('Response complete')).toBeVisible();
    expect(screen.getByRole('log')).toHaveTextContent('Partial');
  });
  it('does not invent cancellation success from an acknowledgement', async () => {
    setup(); await start();
    fireEvent.click(screen.getByRole('button', { name: 'Stop generation' }));
    await screen.findByText('Cancellation unconfirmed');
    expect(screen.getByText(/server has not confirmed a terminal state/i)).toBeVisible();
  });
  it('fails closed on synthetic qualification with no single-host fallback', async () => {
    const client = new TestClient();
    client.current = { ...qualification, evidence_class: 'synthetic_test_fixture', route_ready: false, reason_codes: ['physical_qualification_missing'] };
    setup(client);
    fireEvent.change(screen.getByLabelText('Your prompt'), { target: { value: 'Do not submit' } });
    await screen.findByText(/Test fixture — not physical qualification/);
    expect(screen.getByRole('button', { name: 'Send prompt' })).toBeDisabled();
    expect(client.submitted).toHaveLength(0);
  });
  it('rechecks qualification and requires review of a changed binding before retry', async () => {
    const { client } = setup(); await start();
    await act(async () => { client.event(0, { type: 'accepted' }); client.event(1, { type: 'failed', code: 'route_restarted' }); });
    client.current = { ...qualification, binding: { ...qualification.binding, qualification_id: 'changed-test-binding' } };
    fireEvent.click(screen.getByRole('button', { name: 'Retry prompt' }));
    await screen.findByRole('button', { name: 'Accept current binding' });
    expect(client.submitted).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Accept current binding' }));
    fireEvent.click(screen.getByRole('button', { name: 'Retry prompt' }));
    await waitFor(() => expect(client.submitted).toHaveLength(2));
  });
  it('labels reconnecting evidence independently of worker restart and never presents fixture peers as live Macs', async () => {
    const projection = { protocol: 'mycelium.route_explorer.v1', generation: 1, source_mode: 'fixture', source_digest: hash, digest: hash, overlays: 'unavailable', nodes: [], segments: [] };
    evidence.latest = evidence.visible = { status: 'connecting', freshness: 'stale', source_mode: 'fixture', snapshot: { route_explorer: projection } };
    const { rerender } = setup();
    expect(screen.getByText('Reconnecting evidence feed')).toBeVisible();
    expect(screen.getByText(/Test fixture · not live route evidence/)).toBeVisible();
    evidence.latest = evidence.visible = { ...evidence.latest, status: 'connected', freshness: 'current', source_mode: 'live', snapshot: { route_explorer: { ...projection, source_mode: 'live', generation: 2 } } };
    rerender(<ConsumerDemoPage client={new TestClient()} now={() => NOW} />);
    expect(screen.getByText('Evidence feed connected')).toBeVisible();
    expect(screen.getByLabelText('Published peers')).toHaveTextContent('0');
    expect(screen.queryByText(/Two Macs connected/)).not.toBeInTheDocument();
    // The model identity is published twice: the request header and the route panel.
    await waitFor(() => expect(screen.getAllByText('test-only-model').length).toBeGreaterThan(0));
  });
});
