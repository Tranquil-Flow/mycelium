import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import fixture from '../../../../../contracts/compatibility-fixtures/product-snapshot-v1.json';
import { ProductEvidenceProvider } from './ProductEvidenceContext';
import { ProductEvidenceWorkspace } from './ProductEvidenceWorkspace';
import { decodeProductSnapshot } from './contracts';
import type { ProductEvidenceState } from './source';

const state: ProductEvidenceState = {
  status: 'connected',
  source_mode: 'fixture',
  freshness: 'current',
  generation: 1,
  cursor: 1,
  snapshot: decodeProductSnapshot(fixture),
  reason_code: null,
};

const source = {
  getState: () => state,
  loadInitial: async () => state,
  subscribe: (listener: (value: ProductEvidenceState) => void) => {
    listener(state);
    return () => undefined;
  },
};

describe('unified product workspaces', () => {
  it('renders a mobile member without claiming activation eligibility', async () => {
    render(<ProductEvidenceProvider source={source}><ProductEvidenceWorkspace view="nodes" /></ProductEvidenceProvider>);
    expect(await screen.findByText('android_termux_iroh')).toBeInTheDocument();
    expect(screen.getByText('No')).toBeInTheDocument();
  });

  it('renders the independent readiness matrix from the same snapshot', async () => {
    render(<ProductEvidenceProvider source={source}><ProductEvidenceWorkspace view="readiness" /></ProductEvidenceProvider>);
    expect(await screen.findByText('Independent readiness matrix')).toBeInTheDocument();
    expect(screen.getByText('mobile_qualification_required')).toBeInTheDocument();
  });

  it('toggles logical execution independently from physical links', async () => {
    render(<ProductEvidenceProvider source={source}><ProductEvidenceWorkspace view="network" /></ProductEvidenceProvider>);
    expect(await screen.findByText('Logical execution pipeline')).toBeInTheDocument();
    expect(screen.getByText('Physical directed links')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Logical execution' }));

    expect(screen.queryByText('Logical execution pipeline')).not.toBeInTheDocument();
    expect(screen.getByText('Physical directed links')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Logical execution' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('exposes closed source provenance and a bounded product timeline', async () => {
    render(<ProductEvidenceProvider source={source}><ProductEvidenceWorkspace view="incidents" /></ProductEvidenceProvider>);
    expect(await screen.findByText('Product evidence timeline')).toBeInTheDocument();
    expect(screen.getByText(/cursor 1 · generation 1 · fixture · connected\/current/i)).toBeInTheDocument();
    expect(screen.getByText('Source provenance')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Source provenance'));
    expect(screen.getByText('seed_coordinator')).toBeInTheDocument();
  });
});
