import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { loadStaticObservatoryBundle } from '../../data/observatorySource';
import { EvidenceView } from '../../views/EvidenceView';

describe('EvidenceView', () => {
  const bundle = loadStaticObservatoryBundle();

  function renderView() {
    render(
      <EvidenceView
        snapshot={bundle.snapshot}
        incidents={bundle.incidents}
        provisioning={bundle.provisioning}
      />,
    );
  }

  it('renders the strict ladder and node-by-stage matrix as a semantic table', () => {
    renderView();
    const matrix = screen.getByRole('table', { name: /node-by-stage readiness matrix/i });

    for (const stage of [
      'Discovered',
      'Planned',
      'Assigned',
      'Artifacts verified',
      'Runtime loaded',
      'Stage probed',
      'Route challenged',
      'Route ready',
    ]) {
      expect(within(matrix).getByRole('columnheader', { name: stage })).toBeInTheDocument();
    }
    expect(within(matrix).getAllByText(/✓ proven/i).length).toBeGreaterThan(0);
    expect(within(matrix).getAllByText(/— not proven/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/ready for runtime load is not runtime loaded/i)).toBeInTheDocument();
  });

  it('opens a keyboard-operable source drawer with validation and unknown metadata explicit', () => {
    renderView();
    const toggle = screen.getByRole('button', { name: /open source & evidence drawer/i });

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const drawer = screen.getByRole('region', { name: /source & evidence drawer/i });
    expect(within(drawer).getByText(/protocol/i)).toBeInTheDocument();
    expect(within(drawer).getAllByText(/raw digest not supplied/i).length).toBeGreaterThan(0);
    expect(within(drawer).getAllByText(/validated/i).length).toBeGreaterThan(0);
    expect(within(drawer).getByText(/claim boundary/i)).toBeInTheDocument();
  });

  it('replays only supplied timeline facts and identifies absent comparable history', () => {
    renderView();
    const replay = screen.getByRole('region', { name: /evidence timeline replay/i });

    expect(within(replay).getByRole('slider', { name: /evidence replay position/i })).toBeInTheDocument();
    expect(within(replay).getByText(/supplied evidence event/i)).toBeInTheDocument();
    expect(within(replay).getByText(/no prior comparable capture/i)).toBeInTheDocument();
    fireEvent.click(within(replay).getByRole('button', { name: /previous evidence event/i }));
    expect(within(replay).getByRole('status')).toHaveTextContent(/event \d+ of \d+/i);
  });

  it('creates a downloadable pseudonymized preview without exposing source node ids', () => {
    renderView();
    fireEvent.click(screen.getByRole('button', { name: /create pseudonymized export/i }));

    const preview = screen.getByRole('region', { name: /pseudonymized export preview/i });
    expect(within(preview).getByText(/node-001/i)).toBeInTheDocument();
    expect(preview).not.toHaveTextContent(bundle.snapshot.nodes[0].id);
    expect(screen.getByRole('link', { name: /download pseudonymized json/i })).toHaveAttribute(
      'download',
      'mycelium-evidence-pseudonymized.json',
    );
  });
});
