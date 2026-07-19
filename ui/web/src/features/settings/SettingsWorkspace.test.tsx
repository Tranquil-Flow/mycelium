import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { SettingsProvider } from './SettingsContext';
import { SettingsWorkspace } from './SettingsWorkspace';

describe('SettingsWorkspace', () => {
  beforeEach(() => localStorage.clear());
  it('stores presentation and privacy preferences without secret-shaped keys', () => {
    render(<SettingsProvider><SettingsWorkspace /></SettingsProvider>);
    fireEvent.click(screen.getByRole('checkbox', { name: /reduce motion/i }));
    expect(document.documentElement.dataset.reducedMotion).toBe('true');
    const stored = localStorage.getItem('mycelium.product-ui.preferences.v1') ?? '';
    expect(stored).not.toMatch(/token|credential|invite|endpoint|prompt|output/i);
    expect(screen.getByText(/credentials.*never persisted/i)).toBeInTheDocument();
  });
});
