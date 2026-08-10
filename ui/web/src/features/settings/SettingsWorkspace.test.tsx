import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { SettingsProvider } from './SettingsContext';
import { SettingsWorkspace } from './SettingsWorkspace';
import fixture from '../../../../../contracts/compatibility-fixtures/m15-plan-comparison-v1.json';
import { decodeM15PlanComparison } from '../liveRoute/m15Comparison';

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
});
