import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../App';
import { createProductFeatureRegistry } from './navigation';

describe('product shell feature slots', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '#inference');
  });

  it('renders all stable product routes and the integrated inference workspace', () => {
    render(<App />);

    expect(screen.getByRole('heading', { name: 'Mycelium' })).toBeInTheDocument();
    const navigation = screen.getByRole('navigation', { name: /product sections/i });
    for (const name of [
      'Inference',
      'Network',
      'Nodes',
      'Plans',
      'Readiness',
      'Incidents',
      'Settings',
    ]) {
      expect(navigation.querySelector(`a[href="#${name.toLowerCase()}"]`)).not.toBeNull();
    }
    expect(screen.getByRole('heading', { name: /^inference$/i })).toBeInTheDocument();
    expect(screen.getByText(/session memory only/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /start inference/i })).toBeDisabled();
  });

  it('lazy-loads only the active registered feature module', async () => {
    window.history.replaceState(null, '', '#settings');
    const inferenceLoader = vi.fn(async () => ({
      default: () => <h2>Injected inference feature</h2>,
    }));
    const settingsLoader = vi.fn(async () => ({
      default: () => <h2>Injected settings feature</h2>,
    }));
    const registry = createProductFeatureRegistry([
      { id: 'inference', load: inferenceLoader },
      { id: 'settings', load: settingsLoader },
    ]);

    render(<App featureRegistry={registry} />);
    await screen.findByRole('heading', { name: /injected settings feature/i });

    expect(inferenceLoader).not.toHaveBeenCalled();
    expect(settingsLoader).toHaveBeenCalledTimes(1);
  });

  it('never loads a registered inference module while route readiness is false', () => {
    const inferenceLoader = vi.fn(async () => ({
      default: () => <button type="button">Unsafe inference</button>,
    }));
    render(
      <App
        featureRegistry={createProductFeatureRegistry([
          { id: 'inference', load: inferenceLoader },
        ])}
      />,
    );

    expect(inferenceLoader).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /unsafe inference/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /start inference/i })).toBeDisabled();
  });

  it('normalizes the legacy evidence hash to the readiness route', () => {
    window.history.replaceState(null, '', '#evidence');
    render(<App />);

    expect(screen.getByRole('link', { name: /readiness/i })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(window.location.hash).toBe('#readiness');
  });

  it('falls back from unknown hashes to inference without claiming readiness', () => {
    window.history.replaceState(null, '', '#future-contract');
    render(<App />);

    expect(window.location.hash).toBe('#inference');
    expect(screen.getAllByText(/route readiness unknown/i)).not.toHaveLength(0);
  });

  it('responds to hash changes across stable slots', async () => {
    render(<App />);
    window.history.pushState(null, '', '#settings');
    act(() => window.dispatchEvent(new HashChangeEvent('hashchange')));

    expect(screen.getByRole('heading', { name: /^settings$/i })).toBeInTheDocument();
  });
});
