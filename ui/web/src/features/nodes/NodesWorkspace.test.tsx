import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { loadStaticObservatoryBundle } from '../../data/observatorySource';
import { NodesWorkspace } from './NodesWorkspace';

describe('NodesWorkspace', () => {
  const { snapshot, provisioning } = loadStaticObservatoryBundle();

  it('provides a searchable sortable semantic inventory and keyboard-operable detail', () => {
    render(<NodesWorkspace snapshot={snapshot} provisioning={provisioning} />);

    const table = screen.getByRole('table', { name: /node inventory/i });
    expect(within(table).getAllByRole('row').length).toBe(snapshot.nodes.length + provisioning.nodeIds.length + 1);

    fireEvent.change(screen.getByRole('searchbox', { name: /search nodes/i }), {
      target: { value: 'artifact provisioning' },
    });
    expect(within(table).getAllByRole('row')).toHaveLength(provisioning.nodeIds.length + 1);

    fireEvent.click(screen.getByRole('button', { name: /sort by node/i }));
    fireEvent.click(within(table).getAllByRole('button', { name: /inspect node/i })[0]);
    expect(screen.getByRole('region', { name: /node detail/i })).toBeInTheDocument();
    expect(screen.getByText(/allowlisted redacted projection/i)).toBeInTheDocument();
  });

  it('shows artifact and runtime states independently with non-color text', () => {
    render(<NodesWorkspace snapshot={snapshot} provisioning={provisioning} />);
    fireEvent.change(screen.getByRole('searchbox', { name: /search nodes/i }), {
      target: { value: 'artifact provisioning' },
    });

    expect(screen.getAllByText('PROVEN').length).toBeGreaterThan(0);
    expect(screen.getAllByText('NOT PROVEN').length).toBeGreaterThan(0);
    expect(screen.getByText(/identities are not merged/i)).toBeInTheDocument();
  });
});
