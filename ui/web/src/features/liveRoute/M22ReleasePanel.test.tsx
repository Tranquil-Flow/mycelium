import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { M22ReleasePanel } from './M22ReleasePanel';
import { m22ReleaseFixture } from './m22ReleaseFixtures';

describe('M22 release panel', () => {
  it('shows the larger qualified model and the honest Qwen3-8B blocker in Plans', () => {
    render(<M22ReleasePanel evidence={m22ReleaseFixture} view="plans" />);
    expect(screen.getByText(/Qwen\/Qwen2.5-3B-Instruct/)).toBeVisible();
    expect(screen.getByText(/Qwen3-8B adapter/i)).toBeVisible();
    expect(screen.getByText(/insufficient_swarm_memory_and_disk/i)).toBeVisible();
  });

  it('shows the three-host two-runtime proof without a Tailscale dependency', () => {
    render(<M22ReleasePanel evidence={m22ReleaseFixture} view="network" />);
    expect(screen.getByText('3')).toBeVisible();
    expect(screen.getByText('2')).toBeVisible();
    expect(screen.getByText('endpointid_authenticated_iroh')).toBeVisible();
    expect(screen.getByText('No')).toBeVisible();
  });

  it('surfaces service renewal and reviewer preflight in their product workspaces', () => {
    const { rerender } = render(<M22ReleasePanel evidence={m22ReleaseFixture} view="nodes" />);
    expect(screen.getByText(/jittered \+ bounded reconnect/i)).toBeVisible();
    expect(screen.getByText('Foreground route restart')).toBeVisible();
    expect(screen.getByText('Managed-service restart')).toBeVisible();
    rerender(<M22ReleasePanel evidence={m22ReleaseFixture} view="lab" />);
    expect(screen.getByText(/astras-macbook-m22-1/i)).toBeVisible();
    expect(screen.getAllByText('Verified').length).toBeGreaterThan(0);
  });
});
