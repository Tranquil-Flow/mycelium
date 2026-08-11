import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../App';
import { createProductFeatureRegistry } from './navigation';

describe('product shell feature slots', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '#inference');
  });

  it('renders all stable product routes and the integrated inference workspace', () => {
    render(<App />);

    expect(screen.getByText('Mycelium')).toBeInTheDocument();
    const navigation = screen.getByRole('navigation', { name: /product sections/i });
    for (const name of [
      'Inference',
      'Device Lab',
      'Network',
      'Nodes',
      'Plans',
      'Readiness',
      'Incidents',
      'Settings',
    ]) {
      const hash = name === 'Device Lab' ? 'lab' : name.toLowerCase();
      expect(navigation.querySelector(`a[href="#${hash}"]`)).not.toBeNull();
    }
    expect(screen.getByRole('heading', { name: /^inference$/i })).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByText(/tab-session history/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /start inference/i })).toBeDisabled();
  });

  it('focuses the page region and disables fixture mutation affordances', async () => {
    render(<App />);
    fireEvent.click(screen.getByRole('link', { name: /^nodes/i }));

    expect(document.getElementById('main-content')).toHaveFocus();
    expect(screen.getByRole('heading', { level: 1, name: 'Nodes' })).toBeInTheDocument();
    for (const name of [
      /create native-node invite/i,
      /create browser-probe invite/i,
      /leave fixture-native-node/i,
    ]) {
      expect(screen.getByRole('button', { name })).toBeDisabled();
    }
    expect(screen.getAllByText(/unavailable in offline evidence mode/i).length).toBeGreaterThan(0);
    await waitFor(() => expect(window.location.hash).toBe('#nodes'));
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

  it('keeps Device Lab separate from disabled production Inference', () => {
    window.history.replaceState(null, '', '#lab');
    render(<App />);

    expect(screen.getByRole('heading', { name: /device lab/i })).toBeInTheDocument();
    expect(screen.getByText(/operator capability missing/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: /^inference/i }));
    expect(screen.getByRole('button', { name: /start inference/i })).toBeDisabled();
    expect(screen.getAllByText(/route readiness unknown/i)).not.toHaveLength(0);
  });

  it('allows a directly mounted App to receive an in-memory Device Lab capability', async () => {
    window.history.replaceState(null, '', '#lab');
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('lab offline'));
    const storageSpy = vi.spyOn(Storage.prototype, 'setItem');

    render(<App deviceLabOperatorToken="direct-memory-token" />);

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    expect(new Headers(fetchSpy.mock.calls[0][1]?.headers).get('authorization')).toBe(
      'Bearer direct-memory-token',
    );
    expect(window.location.href).not.toContain('direct-memory-token');
    expect(JSON.stringify(storageSpy.mock.calls)).not.toContain('direct-memory-token');
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
