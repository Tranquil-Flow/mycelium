import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SettingsProvider } from './SettingsContext';
import { SettingsWorkspace } from './SettingsWorkspace';
import fixture from '../../../../../contracts/compatibility-fixtures/m15-plan-comparison-v1.json';
import { decodeM15PlanComparison } from '../liveRoute/m15Comparison';
import { decodeM20SpeculativePlan, decodeM20SpeculativeRuntime } from '../liveRoute/m20Speculation';
import { m20PlanFixture, m20RuntimeFixture } from '../liveRoute/m20SpeculationFixtures';

describe('SettingsWorkspace', () => {
  beforeEach(() => localStorage.clear());
  it('stores presentation and privacy preferences without secret-shaped keys', async () => {
    render(<SettingsProvider><SettingsWorkspace /></SettingsProvider>);
    fireEvent.click(screen.getByRole('checkbox', { name: /reduce motion/i }));
    expect(document.documentElement.dataset.reducedMotion).toBe('true');
    fireEvent.click(screen.getByRole('checkbox', { name: /high contrast/i }));
    expect(document.documentElement.dataset.highContrast).toBe('true');
    fireEvent.change(screen.getByRole('combobox', { name: /density/i }), {
      target: { value: 'compact' },
    });
    expect(document.documentElement.dataset.density).toBe('compact');

    const concealment = screen.getByRole('checkbox', {
      name: /conceal endpoint and network identity/i,
    });
    expect(concealment).toBeChecked();
    fireEvent.click(concealment);
    expect(concealment).not.toBeChecked();
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('mycelium.product-ui.preferences.v1') ?? '{}'))
        .toMatchObject({ concealNetworkIdentity: false });
    });

    const stored = localStorage.getItem('mycelium.product-ui.preferences.v1') ?? '';
    expect(stored).not.toMatch(/token|credential|invite|endpoint|prompt|output/i);
    expect(screen.getByText(/credentials.*never persisted/i)).toBeInTheDocument();
  });

  it('offers only M15-qualified defaults and labels them as future-request intent', async () => {
    const workloadClient = { load: async () => decodeM15PlanComparison(structuredClone(fixture)) };
    render(<SettingsProvider><SettingsWorkspace workloadClient={workloadClient} /></SettingsProvider>);
    const selector = await screen.findByRole('combobox', { name: /default workload and qos profile/i });
    expect(selector).toHaveTextContent('interactive_chat_v1');
    expect(selector).toHaveTextContent('sustained_batch_v1');
    fireEvent.change(selector, { target: { value: 'sustained_batch_v1' } });
    await waitFor(() => expect(localStorage.getItem('mycelium.product-ui.preferences.v1')).toContain('sustained_batch_v1'));
    expect(screen.getByText(/future inference requests/i)).toBeInTheDocument();
    expect(screen.getByText(/does not imply.*admission.*queueing.*batching/i)).toBeInTheDocument();
  });

  it('keeps speculative preference disabled with the measured M20 reason', async () => {
    const speculationClient = { load: async () => [decodeM20SpeculativePlan(structuredClone(m20PlanFixture)), decodeM20SpeculativeRuntime(structuredClone(m20RuntimeFixture))] as const };
    render(<SettingsProvider><SettingsWorkspace speculationClient={speculationClient} /></SettingsProvider>);
    const preference = await screen.findByRole('checkbox', { name: /prefer the qualified draft overlay/i });
    expect(preference).toBeDisabled();
    expect(screen.getByText(/batched target verification unavailable/i)).toBeInTheDocument();
  });

  it('stores only a qualifier-listed model deployment preference', async () => {
    const registry = {
      protocol: 'mycelium.live_deployment_registry.v1' as const,
      selected_deployment_id: 'deployment-a', switching_allowed: true,
      deployments: [
        { deployment_id: 'deployment-a', model_id: 'Qwen/Qwen2.5-0.5B-Instruct', model_revision: 'a'.repeat(40), quantization: 'int8-weight-only', topology_size: 2, health: 'qualified' as const, qualified_at_unix_ms: 1, qualification_id: `sha256:${'a'.repeat(64)}` },
        { deployment_id: 'deployment-b', model_id: 'Qwen/Qwen3-8B', model_revision: 'b'.repeat(40), quantization: 'bfloat16', topology_size: 3, health: 'unavailable' as const, qualified_at_unix_ms: 2, qualification_id: `sha256:${'b'.repeat(64)}` },
      ],
    };
    const select = vi.fn(async () => registry);
    const deploymentClient = { status: async () => registry, select };
    render(<SettingsProvider><SettingsWorkspace deploymentClient={deploymentClient} /></SettingsProvider>);

    const selector = await screen.findByRole('combobox', { name: /preferred qualified model and deployment/i });
    expect(selector).toHaveTextContent('Qwen/Qwen2.5-0.5B-Instruct');
    expect(selector).not.toHaveTextContent('Qwen/Qwen3-8B');
    fireEvent.change(selector, { target: { value: 'deployment-a' } });

    await waitFor(() => expect(select).toHaveBeenCalledWith('deployment-a'));
    await waitFor(() => expect(localStorage.getItem('mycelium.product-ui.preferences.v1')).toContain('deployment-a'));
    expect(screen.getByText(/never rebinds an in-flight request/i)).toBeInTheDocument();
  });
});
