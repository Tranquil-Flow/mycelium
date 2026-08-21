// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { InferencePathBadge } from './InferencePathBadge';
import type { PathClass } from './types';

describe('InferencePathBadge (Inference workspace)', () => {
  it('shows the observed path class for a qualified direct route', () => {
    render(<InferencePathBadge path_class="direct" route_qualified={true} />);
    expect(screen.getByLabelText('observed path class').textContent).toBe('direct');
    expect(screen.queryByText(/membership alone/i)).toBeNull();
  });

  it('shows unknown without any path claim when unmeasured', () => {
    render(<InferencePathBadge path_class="unknown" route_qualified={false} />);
    expect(screen.getByLabelText('observed path class').textContent).toBe('unknown');
    expect(screen.getByText(/membership alone does not make inference available/i)).toBeTruthy();
  });

  it('renders membership alone is not enough for a relay path without qualification', () => {
    render(<InferencePathBadge path_class="relay" route_qualified={false} />);
    expect(screen.getByLabelText('observed path class').textContent).toBe('relay');
    expect(screen.getByText(/membership alone does not make inference available/i)).toBeTruthy();
  });

  it('rejects out-of-vocabulary path classes', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(() => render(<InferencePathBadge path_class={'configured' as any} route_qualified={false} />)).toThrow();
  });

  it('renders no network identity anywhere', () => {
    const { container } = render(
      <InferencePathBadge path_class="relay" route_qualified={false} />,
    );
    expect(container.textContent).not.toMatch(/https?:\/\//);
    expect(container.textContent).not.toMatch(/\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/);
  });
});
