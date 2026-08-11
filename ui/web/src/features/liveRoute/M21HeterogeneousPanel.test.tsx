import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { M21HeterogeneousPanel } from './M21HeterogeneousPanel';
import { decodeM21Heterogeneous } from './m21Heterogeneous';
import { m21HeterogeneousFixture } from './m21HeterogeneousFixtures';

describe('M21HeterogeneousPanel', () => {
  it('shows eligible heterogeneous runtimes and the excluded browser class', () => {
    render(<M21HeterogeneousPanel evidence={decodeM21Heterogeneous(structuredClone(m21HeterogeneousFixture))} view="nodes" />);
    const panel = screen.getByLabelText(/nodes heterogeneous swarm evidence/i);
    expect(within(panel).getByText('mac_mlx_iroh')).toBeInTheDocument();
    expect(within(panel).getByText('linux_numpy_iroh')).toBeInTheDocument();
    expect(within(panel).getByText(/ineligible · activation protocol unavailable/i)).toBeInTheDocument();
  });

  it('shows the product transport truth on the network view', () => {
    render(<M21HeterogeneousPanel evidence={decodeM21Heterogeneous(structuredClone(m21HeterogeneousFixture))} view="network" />);
    expect(screen.getByText('endpointid authenticated iroh')).toBeInTheDocument();
    expect(screen.getByText('peer-mac → peer-linux')).toBeInTheDocument();
  });
});
