import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
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
import type {
  DeploymentRegistryClient,
  DeploymentRegistryStatus,
} from './deploymentClient';
import m15Fixture from '../../../../../contracts/compatibility-fixtures/m15-plan-comparison-v1.json';
import { decodeM15PlanComparison } from '../liveRoute/m15Comparison';
import { decodeM16RuntimeStatus } from '../liveRoute/m16Runtime';
import { m16RuntimeFixture } from '../liveRoute/m16Runtime.test';
import { DEPLOYMENTS_CHANGED_EVENT } from '../liveRoute/deploymentActivation';

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

  constructor(public current: ProductQualification) {}

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

class WaitingWorkspaceClient extends WorkspaceClient {
  override async stream(
    _request: InferenceAcceptedResponse,
    _lastEventId: number | null,
    onEvent: (value: InferenceEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    onEvent({
      protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
      request_id: accepted.request_id,
      sequence: 0,
      type: 'accepted',
    });
    await new Promise<void>((resolve) => {
      if (signal?.aborted === true) resolve();
      else signal?.addEventListener('abort', () => resolve(), { once: true });
    });
  }
}

class UnavailableQualificationClient extends WorkspaceClient {
  override async loadQualification(): Promise<ProductQualification> {
    throw new Error('qualification_unavailable');
  }
}

describe('InferenceWorkspace', () => {
  beforeEach(() => window.sessionStorage.clear());

  it('enables atomic model selection only for multiple qualified deployments', async () => {
    const current: DeploymentRegistryStatus = {
      protocol: 'mycelium.live_deployment_registry.v1',
      selected_deployment_id: 'deployment-a',
      switching_allowed: true,
      deployments: [
        {
          deployment_id: 'deployment-a', model_id: 'Qwen/Qwen2.5-0.5B-Instruct',
          model_revision: 'a'.repeat(40),
          quantization: 'int8-weight-only', topology_size: 2, health: 'qualified',
          qualified_at_unix_ms: NOW,
          qualification_id: 'qualification-a',
        },
        {
          deployment_id: 'deployment-b', model_id: 'Qwen/Qwen2.5-1.5B-Instruct',
          model_revision: 'b'.repeat(40),
          quantization: 'int8-weight-only', topology_size: 2, health: 'qualified',
          qualified_at_unix_ms: NOW + 1,
          qualification_id: 'qualification-b',
        },
      ],
    };
    const select = vi.fn(async (deploymentId: string) => ({
      ...current,
      selected_deployment_id: deploymentId,
    }));
    const deploymentClient: DeploymentRegistryClient = {
      status: async () => current,
      select,
    };
    render(
      <InferenceWorkspace
        client={new WorkspaceClient(qualification(true))}
        deploymentClient={deploymentClient}
        now={() => NOW + 2}
      />,
    );

    const selector = await screen.findByLabelText('Model');
    await waitFor(() => expect(selector).toBeEnabled());
    fireEvent.change(selector, { target: { value: 'deployment-b' } });

    await waitFor(() => expect(select).toHaveBeenCalledWith('deployment-b'));
    expect(screen.getByText(/Switching is atomic and disabled while a request is active/)).toBeVisible();
  });

  it('labels a stale selected deployment without claiming it is qualified', async () => {
    const registry: DeploymentRegistryStatus = {
      protocol: 'mycelium.live_deployment_registry.v1',
      selected_deployment_id: 'deployment-a',
      switching_allowed: true,
      deployments: [
        {
          deployment_id: 'deployment-a', model_id: 'Qwen/Qwen2.5-0.5B-Instruct',
          model_revision: 'a'.repeat(40), quantization: 'int8-weight-only',
          topology_size: 2, health: 'unavailable', qualified_at_unix_ms: NOW,
          qualification_id: 'qualification-a',
        },
      ],
    };
    render(
      <InferenceWorkspace
        client={new UnavailableQualificationClient(qualification(true))}
        deploymentClient={{ status: async () => registry, select: async () => registry }}
        now={() => NOW + 2}
      />,
    );

    expect(await screen.findByText(/is the last selected deployment, but no model is currently qualified/)).toBeVisible();
    expect(screen.queryByText(/It is the only model currently qualified/)).not.toBeInTheDocument();
    expect(screen.getByText('Qwen/Qwen2.5-0.5B-Instruct', { selector: 'strong' })).toBeVisible();
  });

  it('reconciles an externally selected deployment with the current qualification', async () => {
    const firstQualification = qualification(true);
    const secondQualification: ProductQualification = {
      ...firstQualification,
      issued_at_unix_ms: NOW + 1,
      binding: {
        ...firstQualification.binding,
        qualification_id: 'qualification-b',
        deployment_id: 'deployment-b',
        model_id: 'model-b',
      },
    };
    const client = new WorkspaceClient(firstQualification);
    let selected = 'deployment-a';
    const registry = (): DeploymentRegistryStatus => ({
      protocol: 'mycelium.live_deployment_registry.v1',
      selected_deployment_id: selected,
      switching_allowed: true,
      deployments: [
        {
          deployment_id: 'deployment-a', model_id: 'model-a', model_revision: 'a'.repeat(40),
          quantization: 'int8-weight-only', topology_size: 2, health: 'qualified',
          qualified_at_unix_ms: NOW, qualification_id: 'qualification-a',
        },
        {
          deployment_id: 'deployment-b', model_id: 'model-b', model_revision: 'b'.repeat(40),
          quantization: 'int8-weight-only', topology_size: 3, health: 'qualified',
          qualified_at_unix_ms: NOW + 1, qualification_id: 'qualification-b',
        },
      ],
    });
    render(
      <InferenceWorkspace
        client={client}
        deploymentClient={{ status: async () => registry(), select: async () => registry() }}
        now={() => NOW + 2}
      />,
    );

    expect(await screen.findByLabelText('Model')).toHaveValue('deployment-a');
    client.current = secondQualification;
    selected = 'deployment-b';
    window.dispatchEvent(new Event(DEPLOYMENTS_CHANGED_EVENT));

    await waitFor(() => expect(screen.getByLabelText('Model')).toHaveValue('deployment-b'));
    expect(await screen.findByRole('button', { name: 'Accept current binding' })).toBeVisible();
    expect(screen.getByText('deployment-b')).toBeVisible();
  });

  it('disables submission for route_ready=false and renders the exact reason', async () => {
    const client = new WorkspaceClient(qualification(false));
    render(<InferenceWorkspace client={client} now={() => NOW + 1} />);

    const start = await screen.findByRole('button', { name: 'Start inference' });
    await waitFor(() => expect(start).toBeDisabled());
    expect(screen.getByText('Route is not ready: physical_qualification_missing')).toBeVisible();
    expect(screen.getByText('No model request was made.')).toBeVisible();
    expect(screen.getByText(/Local \/ synthetic test evidence/)).toBeVisible();
  });

  it('provides bounded keyboard submission, an accessible stream, tab-session privacy, and terminal state', async () => {
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
    expect(screen.getByLabelText('Maximum new tokens')).toHaveValue(8);
    expect(screen.getByText(/prompt and output stay only in this tab/i)).toBeVisible();
    expect(screen.getByText(/Qualified distributed execution/)).toBeVisible();
    const history = screen.getByRole('region', { name: 'Request history' });
    expect(within(history).getByText('private synthetic input', { selector: 'summary' })).toBeVisible();
    expect(within(history).getByText('synthetic output', { selector: 'summary' })).toBeVisible();
    expect(client.submitted).toHaveLength(1);
    expect(storageSpies.every((spy) => spy.mock.calls.length > 0)).toBe(true);
    expect(window.sessionStorage.getItem('mycelium.inference.tab-session.v1')).toContain(
      'synthetic output',
    );
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

  it('shows honest route activity while waiting for the first token', async () => {
    const client = new WaitingWorkspaceClient(qualification(true));
    const rendered = render(
      <InferenceWorkspace
        client={client}
        runtimeClient={{ load: async () => decodeM16RuntimeStatus(m16RuntimeFixture()) }}
        now={() => NOW + 1}
      />,
    );
    fireEvent.change(screen.getByLabelText('Prompt'), {
      target: { value: 'wait for a real first token' },
    });
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Start inference' })).toBeEnabled();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Start inference' }));

    const activity = await screen.findByRole('status', {
      name: 'Distributed route activity',
    });
    expect(activity).toHaveTextContent('Waiting for first token');
    expect(activity).toHaveTextContent('Distributed prefill and first-token decode');
    expect(activity).toHaveTextContent('Server phase queued');
    expect(activity).toHaveTextContent('interactive QoS');
    expect(activity).toHaveTextContent('1 path reservations');
    rendered.unmount();
  });

  it('attributes history to the selected M15 workload, QoS, and robust policy', async () => {
    const client = new WorkspaceClient(qualification(true));
    client.streams.push([
      { protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL, request_id: accepted.request_id, sequence: 0, type: 'accepted' },
      { protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL, request_id: accepted.request_id, sequence: 1, type: 'token', token_index: 0, text: 'batch output' },
      { protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL, request_id: accepted.request_id, sequence: 2, type: 'completed' },
    ]);
    render(
      <InferenceWorkspace
        client={client}
        workloadClient={{ load: async () => decodeM15PlanComparison(structuredClone(m15Fixture)) }}
        now={() => NOW + 1}
      />,
    );
    const workload = await screen.findByLabelText('Workload and QoS policy');
    fireEvent.change(workload, { target: { value: 'sustained_batch_v1' } });
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'batch-attributed prompt' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start inference' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Start inference' }));

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Completed'));
    expect(screen.getByRole('cell', { name: 'sustained_batch_v1 · batch · balanced' })).toBeVisible();
    expect(client.submitted[0]).toMatchObject({
      protocol: 'mycelium.request_gateway.v2',
      workload_profile_id: 'sustained_batch_v1',
      qos_class: 'batch',
    });
    expect(window.sessionStorage.getItem('mycelium.inference.tab-session.v1')).toContain('sustained_batch_v1');
  });

  it('restores completed output and history after the workspace is remounted', async () => {
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
        text: 'restored output',
      },
      {
        protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
        request_id: accepted.request_id,
        sequence: 2,
        type: 'completed',
      },
    ]);
    const first = render(<InferenceWorkspace client={client} now={() => NOW + 1} />);
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'restored prompt' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start inference' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Start inference' }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Completed'));
    first.unmount();

    render(<InferenceWorkspace client={client} now={() => NOW + 1} />);

    expect(await screen.findByRole('log', { name: 'Decoded output' })).toHaveTextContent(
      'restored output',
    );
    expect(screen.getByLabelText('Prompt')).toHaveValue('restored prompt');
    expect(screen.getByRole('cell', { name: 'completed' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Clear session history' }));
    expect(screen.queryByRole('heading', { name: 'Request history' })).not.toBeInTheDocument();
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
