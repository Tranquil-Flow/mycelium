import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { SettingsProvider } from './SettingsContext';
import { SettingsWorkspace } from './SettingsWorkspace';

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
});
