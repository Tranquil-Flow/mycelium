import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { M13PlacementPanel } from './M13PlacementPanel';
import { m13PlacementFixture } from './routeStatusTestFixture';

describe('M13PlacementPanel', () => {
  it.each(['plans', 'network', 'nodes', 'readiness'] as const)('shows the same signed snapshot in %s', (view) => {
    render(<M13PlacementPanel placement={m13PlacementFixture()} view={view} />);
    expect(screen.getByText('planner_v2')).toBeInTheDocument();
    expect(screen.getByText(/signed snapshot generation 9/i)).toBeInTheDocument();
    expect(screen.getAllByText(/node-0/i).length).toBeGreaterThan(0);
  });
});
