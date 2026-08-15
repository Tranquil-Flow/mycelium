import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { DeploymentActivationClient, DeploymentActivationStatus } from '../liveRoute/deploymentActivation';
import type { M17ModelOperation, M17ModelOperationClient } from '../liveRoute/m17ModelOperation';
import { DEPLOYMENTS_CHANGED_EVENT } from '../liveRoute/deploymentActivation';
import { ModelCatalogControlSource } from './ModelCatalogControlSource';

vi.mock('./ModelCatalogControlPanel', () => ({
  ModelCatalogControlPanel: ({ onActivate, onRefresh, actionsAvailable }: { readonly onActivate: (candidateId: string) => void; readonly onRefresh: () => void; readonly actionsAvailable: boolean }) => <div>
    <span>{actionsAvailable ? 'Actions available' : 'Catalogue read only'}</span>
    <button type="button" onClick={() => onActivate('candidate-a')}>Activate fixture</button>
    <button type="button" onClick={onRefresh}>Refresh fixture</button>
  </div>,
}));

const prepared: DeploymentActivationStatus = {
  protocol: 'mycelium.deployment_activation.v1', generation: 1, busy_candidate_id: null, invalid_candidate_count: 0,
  candidates: [{ candidate_id: 'candidate-a', deployment_id: 'candidate-a', model_id: 'Qwen/A', model_revision: 'a'.repeat(40), quantization: 'int8-weight-only', topology_size: 2, plan_digest: `sha256:${'b'.repeat(64)}`, state: 'prepared', phase: null, completed_steps: 0, total_steps: 4, reason_code: null }],
};
const activating: DeploymentActivationStatus = { ...prepared, generation: 2, busy_candidate_id: 'candidate-a', candidates: [{ ...prepared.candidates[0], state: 'activating', phase: 'validating_plan', completed_steps: 1 }] };
const qualified: DeploymentActivationStatus = { ...prepared, generation: 3, candidates: [{ ...prepared.candidates[0], state: 'qualified', completed_steps: 4 }] };

describe('ModelCatalogControlSource', () => {
  it('loads both authorities, activates the exact candidate, and announces newly qualified deployments', async () => {
    const operationClient: M17ModelOperationClient = { load: vi.fn(async () => ({}) as unknown as Promise<M17ModelOperation>) };
    const status = vi.fn().mockResolvedValueOnce(prepared).mockResolvedValue(qualified);
    const activate = vi.fn(async () => activating);
    const activationClient: DeploymentActivationClient = { status, activate, unload: vi.fn(async () => prepared) };
    const changed = vi.fn();
    window.addEventListener(DEPLOYMENTS_CHANGED_EVENT, changed);
    render(<ModelCatalogControlSource operationClient={operationClient} activationClient={activationClient} now={() => 1_000} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Activate fixture' }));
    await waitFor(() => expect(activate).toHaveBeenCalledWith('candidate-a'));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh fixture' }));
    await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
    window.removeEventListener(DEPLOYMENTS_CHANGED_EVENT, changed);
  });

  it('renders the live catalogue when deployment activation is unavailable', async () => {
    const operationClient: M17ModelOperationClient = { load: vi.fn(async () => ({}) as unknown as Promise<M17ModelOperation>) };
    const activationClient: DeploymentActivationClient = {
      status: vi.fn(async () => { throw new Error('deployment_activation_unavailable'); }),
      activate: vi.fn(async () => prepared),
      unload: vi.fn(async () => prepared),
    };

    render(<ModelCatalogControlSource operationClient={operationClient} activationClient={activationClient} now={() => 1_000} />);

    expect(await screen.findByText('Catalogue read only')).toBeInTheDocument();
  });
});
