import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import App from './App';

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

  it('falls back safely from an unknown view hash', () => {
    window.history.replaceState(null, '', '#future-contract');
    render(<App />);

    expect(window.location.hash).toBe('#network');
    expect(screen.getByRole('heading', { name: /network topology/i })).toBeInTheDocument();
  });
});
