import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import App from './App';
import {
  StaticObservatorySource,
  type ObservatoryDataSource,
  type ObservatorySourceListener,
  type ObservatorySourceState,
} from './data/observatorySource';

class InjectedLiveSource implements ObservatoryDataSource {
  readonly kind = 'live' as const;
  readonly calls: string[] = [];
  private readonly listeners = new Set<ObservatorySourceListener>();
  private state: ObservatorySourceState;

  constructor() {
    const staticState = new StaticObservatorySource().loadInitial();
    this.state = {
      status: 'disconnected',
      generation: 7,
      bundle: staticState.bundle,
      reason: 'test disconnect',
    };
  }

  readonly subscribe = (listener: ObservatorySourceListener): (() => void) => {
    this.calls.push('subscribe');
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  loadInitial(): ObservatorySourceState {
    this.calls.push('loadInitial');
    return this.state;
  }

  getState(): ObservatorySourceState {
    return this.state;
  }

  reconnect(): void {
    this.state = {
      status: 'connected',
      generation: 8,
      bundle: this.state.bundle,
    };
    for (const listener of this.listeners) listener(this.state);
  }
}

describe('Network Observatory', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '#network');
  });
  it('makes fixture mode and disabled live integration unmistakable', () => {
    render(<App />);

    expect(screen.getByRole('heading', { name: /network observatory/i })).toBeInTheDocument();
    expect(screen.getByText(/^MVP$/)).toBeInTheDocument();
    expect(screen.getByText(/simulation · fixture/i)).toBeInTheDocument();
    expect(screen.getByText(/current unavailable/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /pipeline/i })).toBeInTheDocument();
  });

  it('shows truthful failover states and route generations', () => {
    render(<App />);
    fireEvent.click(screen.getByRole('link', { name: /incidents/i }));

    expect(screen.getByText(/active failover/i)).toBeInTheDocument();
    expect(screen.getByText(/^old g42$/i)).toBeInTheDocument();
    expect(screen.getByText(/^new g44$/i)).toBeInTheDocument();
    expect(screen.getByText(/circuit break/i)).toBeInTheDocument();
    expect(screen.getByText(/no reroute claimed/i)).toBeInTheDocument();
  });

  it('compares simulator strategies without presenting modeled metrics as measured', () => {
    render(<App />);
    fireEvent.click(screen.getByRole('link', { name: /plans/i }));

    expect(screen.getByRole('heading', { name: /strategy comparison/i })).toBeInTheDocument();
    expect(screen.getByText('global_best_shortest_subset')).toBeInTheDocument();
    expect(screen.getAllByText(/synthetic/i).length).toBeGreaterThan(0);
  });

  it('keeps artifact readiness separate from route readiness and from simulator scope', () => {
    render(<App />);
    fireEvent.click(screen.getByRole('link', { name: /evidence/i }));

    expect(screen.getByRole('heading', { name: /proof matrix/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /independent provisioning capture/i })).toBeInTheDocument();
    expect(screen.getByText(/ready for runtime load/i)).toBeInTheDocument();
    expect(screen.getByText(/route ready/i)).toBeInTheDocument();
    expect(screen.getByText(/artifact provisioning only/i)).toBeInTheDocument();
    expect(screen.getByText(/separate scope.*not.*active simulation/i)).toBeInTheDocument();
    expect(screen.getAllByText(/tiny-random-GPT2Model-sharded/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/manual-provisioning-route-v1\.json/i)).toBeInTheDocument();
  });

  it('labels drain and request-local circuit triggers without calling every peer failed', () => {
    render(<App />);
    fireEvent.click(screen.getByRole('link', { name: /incidents/i }));

    fireEvent.click(screen.getByRole('button', { name: /stable drain/i }));
    expect(screen.getByText(/departing peer/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /circuit break/i }));
    expect(screen.getByText(/request-local trigger/i)).toBeInTheDocument();
  });

  it('loads a directly addressed view and exposes real navigable links', () => {
    window.history.replaceState(null, '', '#evidence');
    render(<App />);

    expect(screen.getByRole('heading', { name: /proof matrix/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /evidence/i })).toHaveAttribute('href', '#evidence');
    expect(screen.getByRole('link', { name: /evidence/i })).toHaveAttribute('aria-current', 'page');

    fireEvent.click(screen.getByRole('link', { name: /plans/i }));
    expect(window.location.hash).toBe('#plans');
    expect(screen.getByRole('heading', { name: /strategy comparison/i })).toBeInTheDocument();
  });

  it('follows browser hash navigation after initial load', () => {
    render(<App />);

    window.history.pushState(null, '', '#evidence');
    act(() => window.dispatchEvent(new HashChangeEvent('hashchange')));

    expect(screen.getByRole('heading', { name: /proof matrix/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /evidence/i })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('falls back safely from an unknown view hash', () => {
    window.history.replaceState(null, '', '#future-contract');
    render(<App />);

    expect(window.location.hash).toBe('#network');
    expect(screen.getByRole('heading', { name: /network topology/i })).toBeInTheDocument();
  });

  it('renders the existing fail-closed fixture error instead of crashing source construction', () => {
    const source = new StaticObservatorySource(() => {
      throw new TypeError('fixture contract mismatch');
    });

    render(<App source={source} />);

    expect(screen.getByText(/offline fixture error/i)).toBeInTheDocument();
    expect(screen.getByText(/fixture contract mismatch/i)).toBeInTheDocument();
    expect(screen.getByText(/no fallback values/i)).toBeInTheDocument();
  });

  it('accepts an injected read-only live source and reflects disconnect then reconnect state', () => {
    const source = new InjectedLiveSource();
    render(<App source={source} />);

    expect(screen.getByText(/live · read only/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /disconnected · g7/i })).toBeDisabled();
    expect(source.calls).toEqual(['subscribe', 'loadInitial']);

    act(() => source.reconnect());

    expect(screen.getByRole('button', { name: /current · g8/i })).toBeDisabled();
  });

  it('never renders a previous source bundle under a replacement source identity', async () => {
    const staticSource = new StaticObservatorySource();
    const bundle = staticSource.loadInitial().bundle;
    let resolveInitial: ((state: ObservatorySourceState) => void) | undefined;
    const loading = new Promise<ObservatorySourceState>((resolve) => {
      resolveInitial = resolve;
    });
    const replacement: ObservatoryDataSource = {
      kind: 'live',
      getState: () => null,
      loadInitial: () => loading,
    };
    const { rerender } = render(<App source={staticSource} />);
    expect(screen.getByRole('heading', { name: /network topology/i })).toBeInTheDocument();

    rerender(<App source={replacement} />);

    expect(
      screen.getByRole('heading', { name: /loading coherent evidence snapshot/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /network topology/i })).not.toBeInTheDocument();
    expect(screen.getByText(/live · read only/i)).toBeInTheDocument();

    await act(async () => {
      resolveInitial?.({ status: 'connected', generation: 12, bundle });
      await loading;
    });

    expect(screen.getByRole('heading', { name: /network topology/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /current · g12/i })).toBeDisabled();
  });
});
