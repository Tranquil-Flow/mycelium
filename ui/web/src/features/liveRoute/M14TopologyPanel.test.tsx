import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { M14TopologyPanel } from './M14TopologyPanel';
import { m14TopologyFixture } from './routeStatusTestFixture';

describe('M14TopologyPanel', () => {
  it('shows the selected cycle, exactness, and nested allocation in Plans', () => {
    render(<M14TopologyPanel topology={m14TopologyFixture()} view="plans" />);
    expect(screen.getByText(/globally exact/i)).toBeInTheDocument();
    expect(screen.getByText(/node-0 → node-1 → node-2; sampled-token closure/i)).toBeInTheDocument();
    expect(screen.getByText(/minimum measured directed RTT\/2 plus jitter/i)).toBeInTheDocument();
    expect(screen.getAllByText(/layers \[/i)).toHaveLength(3);
  });

  it('switches to honest unknown-location map geometry and inspects a measured edge', () => {
    render(<M14TopologyPanel topology={m14TopologyFixture()} view="network" />);
    fireEvent.click(screen.getByRole('button', { name: 'True map' }));
    expect(screen.getByText(/explicit unknown-location bucket/i)).toBeInTheDocument();
    expect(screen.getByText(/RTT \/ 2 \+ jitter/i)).toHaveTextContent('=');
    expect(screen.getByText(/frames \/ 1 opened/i)).toBeInTheDocument();
  });

  it('shows the complete matrix and persistent reuse readiness gates', () => {
    render(<M14TopologyPanel topology={m14TopologyFixture()} view="readiness" />);
    expect(screen.getByText('6 / 6 edges')).toBeInTheDocument();
    expect(screen.getByText('Proven')).toBeInTheDocument();
    expect(screen.getByText('node-2 → node-0')).toBeInTheDocument();
  });
});
