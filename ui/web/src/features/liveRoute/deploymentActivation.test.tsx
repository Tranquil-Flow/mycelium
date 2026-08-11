import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { decodeDeploymentActivationStatus, type DeploymentActivationStatus } from './deploymentActivation';
import { PreparedDeploymentsPanel } from './PreparedDeploymentsPanel';

const status = (overrides: Partial<DeploymentActivationStatus['candidates'][number]> = {}): DeploymentActivationStatus => ({
  protocol: 'mycelium.deployment_activation.v1',
  generation: 3,
  busy_candidate_id: null,
  invalid_candidate_count: 0,
  candidates: [{
    candidate_id: 'candidate-a',
    deployment_id: 'deployment-a',
    model_id: 'Qwen/Qwen2.5-3B-Instruct',
    model_revision: 'a'.repeat(40),
    quantization: 'int8-weight-only',
    topology_size: 3,
    plan_digest: `sha256:${'b'.repeat(64)}`,
    state: 'prepared',
    phase: null,
    completed_steps: 0,
    total_steps: 4,
    reason_code: null,
    ...overrides,
  }],
});

describe('prepared deployment activation', () => {
  it('decodes the closed backend contract and rejects inconsistent progress', () => {
    expect(decodeDeploymentActivationStatus(status()).candidates[0].model_id).toBe('Qwen/Qwen2.5-3B-Instruct');
    expect(() => decodeDeploymentActivationStatus(status({ state: 'activating', phase: null }))).toThrow(/inconsistent/);
    expect(() => decodeDeploymentActivationStatus({ ...status(), private_path: '/tmp/model' })).toThrow(/shape/);
  });

  it('shows a prepared model and requests explicit activation', () => {
    const activate = vi.fn();
    render(<PreparedDeploymentsPanel status={status()} view="inference" activatingCandidateId={null} error={null} onActivate={activate} />);

    expect(screen.getByRole('heading', { name: 'Prepared deployments' })).toBeInTheDocument();
    expect(screen.getByText('Qwen/Qwen2.5-3B-Instruct')).toBeInTheDocument();
    expect(screen.getByText(/never downloads a model/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Activate deployment' }));
    expect(activate).toHaveBeenCalledWith('candidate-a');
  });

  it('renders qualification progress and keeps selection explicit', () => {
    const unload = vi.fn();
    const activating = {
      ...status({
        state: 'activating',
        phase: 'qualifying_route',
        completed_steps: 3,
      }),
      busy_candidate_id: 'candidate-a',
    } as const;
    const { rerender } = render(<PreparedDeploymentsPanel status={activating} view="plans" activatingCandidateId={null} error={null} onActivate={() => undefined} />);
    expect(screen.getByText(/Running the distributed startup challenge/)).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '3');

    rerender(<PreparedDeploymentsPanel status={status({ state: 'qualified', completed_steps: 4 })} view="plans" activatingCandidateId={null} error={null} onActivate={() => undefined} onUnload={unload} />);
    fireEvent.click(screen.getByRole('button', { name: 'Unload from memory' }));
    expect(unload).toHaveBeenCalledWith('candidate-a');
    expect(screen.queryByRole('button', { name: /activate/i })).not.toBeInTheDocument();
  });
});
