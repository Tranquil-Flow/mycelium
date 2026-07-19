import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  PRODUCT_INFERENCE_EVENT_PROTOCOL,
  PRODUCT_INFERENCE_PROTOCOL,
  type InferenceAcceptedResponse,
  type InferenceCancelResponse,
  type InferenceEvent,
  type InferenceSubmission,
  type ProductQualification,
} from '../../app/contracts';
import type { InferenceClient } from './requestClient';
import { InferenceWorkspace } from './InferenceWorkspace';
import workspaceCss from './InferenceWorkspace.module.css?raw';

const NOW = 1_800_000_000_000;
const DIGEST = `sha256:${'a'.repeat(64)}`;

function qualification(routeReady: boolean): ProductQualification {
  return {
    protocol: PRODUCT_INFERENCE_PROTOCOL,
    issued_at_unix_ms: NOW,
    evidence_class: routeReady ? 'physical_qualification' : 'synthetic_test_fixture',
    route_ready: routeReady,
    reason_codes: routeReady ? [] : ['physical_qualification_missing'],
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
  };
}

const accepted: InferenceAcceptedResponse = {
  protocol: PRODUCT_INFERENCE_PROTOCOL,
  request_id: 'request-a',
  accepted: true,
  event_path: '/api/v1/inference/request-a/events',
  cancel_path: '/api/v1/inference/request-a/cancel',
};

class WorkspaceClient implements InferenceClient {
  readonly submitted: InferenceSubmission[] = [];
  streams: InferenceEvent[][] = [];

  constructor(readonly current: ProductQualification) {}

  async loadQualification() {
    return this.current;
  }

  async submit(value: InferenceSubmission) {
    this.submitted.push(value);
    return accepted;
  }

  async stream(
    _request: InferenceAcceptedResponse,
    _lastEventId: number | null,
    onEvent: (value: InferenceEvent) => void,
  ) {
    for (const item of this.streams.shift() ?? []) onEvent(item);
  }

  async cancel(): Promise<InferenceCancelResponse> {
    return {
      protocol: PRODUCT_INFERENCE_PROTOCOL,
      request_id: accepted.request_id,
      cancelled: true,
    };
  }
}

describe('InferenceWorkspace', () => {
  it('disables submission for route_ready=false and renders the exact reason', async () => {
    const client = new WorkspaceClient(qualification(false));
    render(<InferenceWorkspace client={client} now={() => NOW + 1} />);

    const start = await screen.findByRole('button', { name: 'Start inference' });
    await waitFor(() => expect(start).toBeDisabled());
    expect(screen.getByText('Route is not ready: physical_qualification_missing')).toBeVisible();
    expect(screen.getByText(/Local \/ synthetic test evidence/)).toBeVisible();
  });

  it('provides bounded keyboard submission, an accessible stream, privacy defaults, and terminal state', async () => {
    const client = new WorkspaceClient(qualification(true));
    client.streams.push([
      {
        protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
        request_id: accepted.request_id,
        sequence: 0,
        type: 'accepted',
      },
      {
        protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
        request_id: accepted.request_id,
        sequence: 1,
        type: 'token',
        token_index: 0,
        text: 'synthetic output',
      },
      {
        protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
        request_id: accepted.request_id,
        sequence: 2,
        type: 'completed',
      },
    ]);
    const storageSpies = [
      vi.spyOn(Storage.prototype, 'setItem'),
      vi.spyOn(Storage.prototype, 'getItem'),
    ];
    const consoleSpies = [
      vi.spyOn(console, 'debug').mockImplementation(() => undefined),
      vi.spyOn(console, 'info').mockImplementation(() => undefined),
      vi.spyOn(console, 'log').mockImplementation(() => undefined),
      vi.spyOn(console, 'warn').mockImplementation(() => undefined),
      vi.spyOn(console, 'error').mockImplementation(() => undefined),
    ];
    render(<InferenceWorkspace client={client} now={() => NOW + 1} />);
    const editor = screen.getByLabelText('Prompt');
    fireEvent.change(editor, { target: { value: 'private synthetic input' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start inference' })).toBeEnabled());

    fireEvent.keyDown(editor, { key: 'Enter', ctrlKey: true });

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Completed'));
    expect(screen.getByRole('log', { name: 'Decoded output' })).toHaveTextContent(
      'synthetic output',
    );
    expect(screen.getByText(/131072 UTF-8 bytes maximum/)).toBeVisible();
    expect(screen.getByLabelText('Maximum new tokens')).toHaveAttribute('max', '4096');
    expect(screen.getByText(/prompt and output are not persisted/i)).toBeVisible();
    expect(screen.getByText(/Qualified distributed execution/)).toBeVisible();
    expect(client.submitted).toHaveLength(1);
    expect(storageSpies.every((spy) => spy.mock.calls.length === 0)).toBe(true);
    expect(window.location.href).not.toContain('private synthetic input');
    expect(
      consoleSpies.flatMap((spy) => spy.mock.calls).some((call) =>
        call.some((value) =>
          String(value).includes('private synthetic input') ||
          String(value).includes('synthetic output'),
        ),
      ),
    ).toBe(false);
    for (const spy of [...storageSpies, ...consoleSpies]) spy.mockRestore();
  });

  it('exposes stream resume and cancellation as keyboard-operable buttons', async () => {
    const client = new WorkspaceClient(qualification(true));
    client.streams.push([
      {
        protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
        request_id: accepted.request_id,
        sequence: 0,
        type: 'accepted',
      },
    ]);
    render(<InferenceWorkspace client={client} now={() => NOW + 1} />);
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'synthetic input' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start inference' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Start inference' }));

    await waitFor(() => expect(screen.getByRole('button', { name: 'Resume stream' })).toBeEnabled());
    expect(screen.getByRole('button', { name: 'Cancel request' })).toBeEnabled();
  });

  it('ships feature-local reduced-motion and forced-colors safeguards', () => {
    expect(workspaceCss).toMatch(/@media\s*\(prefers-reduced-motion:\s*reduce\)/);
    expect(workspaceCss).toMatch(/@media\s*\(forced-colors:\s*active\)/);
  });
});
