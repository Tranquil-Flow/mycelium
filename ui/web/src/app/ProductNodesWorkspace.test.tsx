import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SettingsProvider } from '../features/settings/SettingsContext';
import { ProductNodesWorkspace } from './ProductNodesWorkspace';

describe('ProductNodesWorkspace', () => {
  it('shows the bounded multi-device operator path in live mode', () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('membership projection unavailable'));
    render(
      <SettingsProvider>
        <ProductNodesWorkspace sourceMode="live" />
      </SettingsProvider>,
    );

    expect(
      screen.getByRole('heading', { name: /ready for multiple invited users and devices/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/up to 64 unique, short-lived, single-use/i)).toBeInTheDocument();
    expect(screen.getByText(/joining never changes the active route/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create native-node invite/i })).toBeEnabled();
    expect(screen.getByText(/complete enrollment on the new device/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /create browser-probe invite/i })).not.toBeInTheDocument();
  });

  it('does not claim live onboarding readiness for a fixture', () => {
    render(
      <SettingsProvider>
        <ProductNodesWorkspace sourceMode="fixture" />
      </SettingsProvider>,
    );

    expect(screen.queryByText(/ready for multiple invited users and devices/i)).not.toBeInTheDocument();
    expect(screen.getAllByText(/unavailable in offline evidence mode/i).length).toBeGreaterThan(0);
  });
});
