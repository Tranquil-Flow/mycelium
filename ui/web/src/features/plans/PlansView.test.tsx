import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { loadStaticObservatoryBundle } from '../../data/observatorySource';
import { PlansView } from '../../views/PlansView';

describe('PlansView', () => {
  const { snapshot } = loadStaticObservatoryBundle();

  it('offers read-only ranking and synchronized comparison with explicit deltas', () => {
    render(<PlansView snapshot={snapshot} />);

    expect(screen.getByRole('heading', { name: /strategy comparison/i })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: /strategy ranking/i })).toBeInTheDocument();
    expect(screen.getAllByText(/modeled · synthetic/i).length).toBeGreaterThan(0);

    const comparison = screen.getByRole('region', { name: /synchronized strategy comparison/i });
    expect(within(comparison).getByText(/delta vs/i)).toBeInTheDocument();
    expect(within(comparison).getAllByText(/[+−]\d/).length).toBeGreaterThan(0);
  });

  it('changes inspection only and exposes no planner mutation controls', () => {
    render(<PlansView snapshot={snapshot} />);
    const candidate = snapshot.routes.find((route) => route.id !== snapshot.routes[0].id)!;

    fireEvent.click(screen.getByRole('button', { name: `Inspect ${candidate.id}` }));

    expect(screen.getByRole('heading', { name: new RegExp(candidate.simulatorStrategy, 'i') })).toBeInTheDocument();
    expect(screen.queryByRole('slider')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /\b(?:run|apply|save|replan)\b/i })).not.toBeInTheDocument();
    expect(screen.getByText(/read-only interpretation/i)).toBeInTheDocument();
  });

  it('renders exact half-open allocations, alternatives, assumptions, bottleneck, and missing trace state', () => {
    render(<PlansView snapshot={snapshot} />);

    expect(screen.getByRole('table', { name: /selected route layer allocation/i })).toBeInTheDocument();
    expect(screen.getAllByText(/^\[\d+,\d+\)$/).length).toBeGreaterThan(0);
    expect(screen.getByRole('heading', { name: /route alternatives/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /model & workload assumptions/i })).toBeInTheDocument();
    expect(screen.getByText(/pruning trace not supplied/i)).toBeInTheDocument();
    expect(screen.getByText(/predicted bottleneck/i)).toBeInTheDocument();
  });
});
