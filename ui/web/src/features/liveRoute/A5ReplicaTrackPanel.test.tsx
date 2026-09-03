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
