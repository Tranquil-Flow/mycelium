import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { M18ReplicationPanel } from './M18ReplicationPanel';
import { decodeM18ReplicaPlan, decodeM18ReplicaRuntime } from './m18Replication';
import { m18PlanFixture, m18RuntimeFixture } from './m18Replication.test';

describe('M18ReplicationPanel', () => {
  it('labels tracks as data parallel and shows immutable request attribution', () => {
    render(<M18ReplicationPanel plan={decodeM18ReplicaPlan(m18PlanFixture())} runtime={decodeM18ReplicaRuntime(m18RuntimeFixture())} view="inference" />);
    expect(screen.getByText('data parallel')).toBeInTheDocument();
    expect(screen.getByText(/single request is never tensor-split/i)).toBeInTheDocument();
    expect(screen.getByText('p0 → p1')).toBeInTheDocument();
    expect(screen.getByText('track pinned')).toBeInTheDocument();
  });

  it('keeps planner intent visibly unpromoted without runtime qualification', () => {
    render(<M18ReplicationPanel plan={decodeM18ReplicaPlan(m18PlanFixture())} runtime={null} view="readiness" />);
    expect(screen.getAllByText(/Planner intent only/i)).toHaveLength(2);
    expect(screen.getByText(/route-ready claim:/i)).toBeInTheDocument();
  });

  it('shows physically measured throughput separately from planner prediction', () => {
    render(<M18ReplicationPanel plan={decodeM18ReplicaPlan(m18PlanFixture())} runtime={decodeM18ReplicaRuntime(m18RuntimeFixture())} view="plans" />);
    expect(screen.getByText('100.0%')).toBeInTheDocument();
  });
});
