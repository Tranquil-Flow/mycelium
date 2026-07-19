import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import {
  StaticObservatorySource,
  type LiveObservatorySourceState,
  type ObservatoryDataSource,
  type ObservatorySourceListener,
  type ObservatorySourceState,
} from './data/observatorySource';
import { decodeObservatorySnapshot } from './model/semanticProjection';
import { validSemanticSnapshot } from './test/semanticFixture';

class InjectedLiveSource implements ObservatoryDataSource {
  readonly source_mode = 'live' as const;
  readonly kind = 'live' as const;
  readonly calls: string[] = [];
  private readonly listeners = new Set<ObservatorySourceListener>();
  private state: LiveObservatorySourceState;

  constructor() {
    this.state = {
      source_mode: 'live',
      status: 'disconnected',
      generation: 7,
      snapshot: decodeObservatorySnapshot(validSemanticSnapshot()),
      live_qualified: false,
      qualification_reasons: ['transport_not_current'],
      freshness: 'current',
      reason: 'test disconnect',
    };
  }

  readonly subscribe = (listener: ObservatorySourceListener): (() => void) => {
    this.calls.push('subscribe');
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  loadInitial(): LiveObservatorySourceState {
    this.calls.push('loadInitial');
    return this.state;
  }

  getState(): LiveObservatorySourceState {
    return this.state;
  }

  reconnect(): void {
    this.state = {
      ...this.state,
      status: 'connected',
      generation: 8,
      live_qualified: true,
      qualification_reasons: [],
      reason: undefined,
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

    expect(screen.getByRole('heading', { name: 'Mycelium' })).toBeInTheDocument();
    expect(screen.getByText(/^MVP$/)).toBeInTheDocument();
    expect(screen.getByText(/fixture data · not live/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /fixture data · not current/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /pipeline/i })).toBeInTheDocument();
  });

  it('wires inference, node/swarm, membership, and settings workspaces without fixture network calls', async () => {
    window.history.replaceState(null, '', '#inference');
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('fixture network forbidden'));
    render(<App />);

    expect(await screen.findByRole('heading', { name: /^inference$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /start inference/i })).toBeDisabled();
    fireEvent.click(screen.getByRole('link', { name: /nodes/i }));
    expect(await screen.findByRole('heading', { name: /nodes and swarm/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /join a trusted swarm/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: /settings/i }));
    expect(screen.getByRole('heading', { name: /^settings$/i })).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
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
    expect(screen.getAllByText(/synthetic/i).length).toBeGreaterThan(0);
  });

  it('keeps artifact readiness separate from route readiness and from simulator scope', () => {
    render(<App />);
    fireEvent.click(screen.getByRole('link', { name: /evidence/i }));

    expect(screen.getByRole('heading', { name: /proof matrix/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /independent provisioning capture/i })).toBeInTheDocument();
    expect(screen.getAllByText(/ready for runtime load/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/route ready/i).length).toBeGreaterThan(0);
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
    expect(screen.getByRole('link', { name: /readiness/i })).toHaveAttribute('href', '#readiness');
    expect(screen.getByRole('link', { name: /readiness/i })).toHaveAttribute('aria-current', 'page');
    expect(window.location.hash).toBe('#readiness');

    fireEvent.click(screen.getByRole('link', { name: /plans/i }));
    expect(window.location.hash).toBe('#plans');
    expect(screen.getByRole('heading', { name: /strategy comparison/i })).toBeInTheDocument();
  });

  it('follows browser hash navigation after initial load', () => {
    render(<App />);

    window.history.pushState(null, '', '#evidence');
    act(() => window.dispatchEvent(new HashChangeEvent('hashchange')));

    expect(screen.getByRole('heading', { name: /proof matrix/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /readiness/i })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('falls back safely from an unknown view hash', () => {
    window.history.replaceState(null, '', '#future-contract');
    render(<App />);

    expect(window.location.hash).toBe('#inference');
    expect(screen.getByRole('heading', { name: /^inference$/i })).toBeInTheDocument();
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

  it('does not label disconnected semantic evidence live, then gates live on qualification', () => {
    const source = new InjectedLiveSource();
    render(<App source={source} />);

    expect(screen.getByText(/live evidence · not current/i)).toBeInTheDocument();
    expect(screen.queryByText(/^live evidence · current$/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /disconnected · g7/i })).toBeDisabled();
    expect(screen.getAllByText(/deployment-alpha/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/route-primary/i)).toBeInTheDocument();
    expect(source.calls).toEqual(['subscribe', 'loadInitial']);

    act(() => source.reconnect());

    expect(screen.getByText(/^live evidence · current$/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /current evidence · g8/i })).toBeDisabled();
  });

  it('never renders a previous fixture bundle under a replacement live source identity', async () => {
    const staticSource = new StaticObservatorySource();
    const semanticState: LiveObservatorySourceState = {
      source_mode: 'live',
      status: 'connected',
      generation: 12,
      snapshot: decodeObservatorySnapshot(validSemanticSnapshot()),
      live_qualified: true,
      qualification_reasons: [],
      freshness: 'current',
    };
    let resolveInitial: ((state: ObservatorySourceState) => void) | undefined;
    const loading = new Promise<ObservatorySourceState>((resolve) => {
      resolveInitial = resolve;
    });
    const replacement: ObservatoryDataSource = {
      source_mode: 'live',
      kind: 'live',
      getState: () => null,
      loadInitial: () => loading,
    };
    const { rerender } = render(<App source={staticSource} />);
    expect(screen.getByRole('heading', { name: /network topology/i })).toBeInTheDocument();

    rerender(<App source={replacement} />);

    expect(
      screen.getByRole('heading', { name: /loading semantic observatory snapshot/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /network topology/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/^live evidence · current$/i)).not.toBeInTheDocument();

    await act(async () => {
      resolveInitial?.(semanticState);
      await loading;
    });

    expect(screen.getAllByText(/deployment-alpha/i).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: /current evidence · g12/i })).toBeDisabled();
    expect(screen.getByText(/^live evidence · current$/i)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /network topology/i })).not.toBeInTheDocument();
  });
});
