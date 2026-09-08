import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { A5ReplicaTrackPanel } from './A5ReplicaTrackPanel';
import { decodeA5ReplicaTrackQualifications } from './a5Replication';
import { a5QualificationFixture } from './routeStatusTestFixture';

describe('A5ReplicaTrackPanel', () => {
  const qualifications = decodeA5ReplicaTrackQualifications([
    a5QualificationFixture(),
    a5QualificationFixture({
      qualification_id: `sha256:${'f'.repeat(64)}`,
      qualification_digest: `sha256:${'f'.repeat(64)}`,
      track_id: 'track-fixture-b',
      placement_id: 'placement-fixture-b',
      placement_ids: ['placement-fixture-b', 'placement-fixture-stage-b'],
    }),
  ]);

  it('labels the panel data parallel and shows request-level copy', () => {
    render(
      <A5ReplicaTrackPanel
        qualifications={qualifications}
        lossPlacementIds={[]}
        view="tracks"
      />,
    );
    expect(screen.getByText('data parallel')).toBeInTheDocument();
    expect(screen.getByText(/exactly one complete legal track/i)).toBeInTheDocument();
  });

  it('shows qualified tracks with replica group and generation', () => {
    render(
      <A5ReplicaTrackPanel
        qualifications={qualifications}
        lossPlacementIds={[]}
        view="tracks"
      />,
    );
    expect(screen.getByText('placement-fixture-replica')).toBeInTheDocument();
    expect(screen.getByText('placement-fixture-b')).toBeInTheDocument();
    expect(screen.getAllByText('group-fixture-0').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('qualified').length).toBeGreaterThanOrEqual(2);
  });

  it('shows qualification evidence checks', () => {
    render(
      <A5ReplicaTrackPanel
        qualifications={qualifications}
        lossPlacementIds={[]}
        view="qualification"
      />,
    );
    expect(screen.getAllByText('pass').length).toBeGreaterThanOrEqual(10);
  });

  it('projects a lost placement as blocked for new admission', () => {
    render(
      <A5ReplicaTrackPanel
        qualifications={qualifications}
        lossPlacementIds={['placement-fixture-stage-1']}
        view="loss"
      />,
    );
    expect(screen.getByText(/1 surviving qualified track/i)).toBeInTheDocument();
    expect(screen.getByText(/1 degraded by placement loss/i)).toBeInTheDocument();
    expect(screen.getByText(/lost — new admission blocked/i)).toBeInTheDocument();
  });

  it('shows the complete ordered placement sequence, not just its replica member', () => {
    render(<A5ReplicaTrackPanel qualifications={qualifications} lossPlacementIds={[]} view="tracks" />);
    expect(screen.getByRole('columnheader', { name: 'Complete placement sequence' })).toBeInTheDocument();
    for (const record of qualifications) expect(screen.getByText(record.placement_ids.join(' → '))).toBeInTheDocument();
  });

  it.each(['tracks', 'qualification', 'loss'] as const)('does not treat future evidence as current in %s', (view) => {
    render(<A5ReplicaTrackPanel qualifications={[qualifications[0]]} lossPlacementIds={[]} view={view} nowUnixMs={qualifications[0].issued_at_unix_ms - 1} />);
    expect(screen.getByText('not yet valid')).toBeInTheDocument();
    expect(screen.queryByText('qualified', { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText('pass', { exact: true })).not.toBeInTheDocument();
    if (view === 'loss') expect(screen.getByText(/0 surviving qualified tracks/)).toBeInTheDocument();
  });

  it('keeps expired readiness checks historical', () => {
    render(<A5ReplicaTrackPanel qualifications={[qualifications[0]]} lossPlacementIds={[]} view="qualification" nowUnixMs={qualifications[0].expires_at_unix_ms + 1} />);
    expect(screen.getByText('expired')).toBeInTheDocument();
    expect(screen.queryByText('pass', { exact: true })).not.toBeInTheDocument();
    expect(screen.getAllByText('recorded pass — not current')).toHaveLength(5);
  });

  it('keeps the authority boundary copy visible', () => {
    render(
      <A5ReplicaTrackPanel
        qualifications={qualifications}
        lossPlacementIds={[]}
        view="loss"
      />,
    );
    expect(screen.getByText(/route-ready for a replica track/i)).toBeInTheDocument();
  });
});
