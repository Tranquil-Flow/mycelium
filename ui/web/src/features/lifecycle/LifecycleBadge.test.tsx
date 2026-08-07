import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import {
  LifecycleBadge,
  LifecycleCoveragePanel,
} from './LifecycleBadge';
import {
  LIFECYCLE_STATE_ORDER,
  projectLifecycle,
  type LifecycleState,
} from './lifecycleProjection';
import { preparingFixture } from '../../test/lifecycleFixtures/recordedSnapshots';

describe('LifecycleBadge', () => {
  it.each(LIFECYCLE_STATE_ORDER)('renders %s visibly with data and accessible status metadata', (state) => {
    const projection = projectLifecycle(preparingFixture({ state }));
    render(<LifecycleBadge projection={projection} />);

    const badge = screen.getByRole('status', { name: projection.accessibility_text });
    expect(badge).toHaveTextContent(projection.label);
    expect(badge).toHaveAttribute('data-lifecycle-state', state);
    expect(badge).toHaveAttribute('data-route-ready', projection.route_ready ? 'true' : 'false');
    expect(badge).toHaveAttribute(
      'data-inference-enabled',
      projection.inference_enabled ? 'true' : 'false',
    );
  });

  it('renders a coverage panel with every recorded lifecycle label without claiming devices', () => {
    const projections = LIFECYCLE_STATE_ORDER.map((state: LifecycleState) =>
      projectLifecycle(preparingFixture({ state })),
    );
    render(<LifecycleCoveragePanel projections={projections} />);

    const panel = screen.getByRole('region', { name: /recorded lifecycle projection coverage/i });
    for (const projection of projections) {
      const row = within(panel).getByTestId(`lifecycle-coverage-${projection.state}`);
      expect(row).toHaveTextContent(projection.label);
      expect(row).toHaveTextContent('recorded_event_projection_only');
      expect(row).toHaveTextContent('real_device=false');
      expect(row).toHaveTextContent('physical_devices_present=0');
      expect(row).toHaveAttribute('data-route-ready', 'false');
      expect(row).toHaveAttribute('data-inference-enabled', 'false');
      const badge = within(row).getByRole('status');
      expect(badge).toHaveAttribute('data-route-ready', 'false');
      expect(badge).toHaveAttribute('data-inference-enabled', 'false');
    }
  });
});
