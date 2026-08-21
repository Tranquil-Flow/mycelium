// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { NodesInternetPanel } from './NodesInternetPanel';
import type { FreshnessState } from './types';

describe('NodesInternetPanel (Nodes workspace)', () => {
  it('renders incarnation, generation, lease freshness, and qualification state', () => {
    render(
      <NodesInternetPanel
        incarnation="node-1-incarnation"
        generation={2}
        endpoint_pseudonym={'sha256:' + 'c'.repeat(64)}
        lease_freshness="current"
        qualification="qualified"
      />,
    );
    expect(screen.getByLabelText('incarnation').textContent).toBe('node-1-incarnation');
    expect(screen.getByLabelText('generation').textContent).toBe('2');
    expect(screen.getByLabelText('lease freshness').textContent).toBe('current');
    expect(screen.getByLabelText('qualification').textContent).toBe('qualified');
    expect(screen.getByLabelText('endpoint').textContent).toBe('sha256:' + 'c'.repeat(64));
  });

  it('never renders a raw endpoint id', () => {
    const raw = '51947b11deadbeef51947b11deadbeef';
    const { container } = render(
      <NodesInternetPanel
        incarnation="node-1-incarnation"
        generation={1}
        endpoint_pseudonym={raw}
        lease_freshness={'unknown' as FreshnessState}
        qualification={null}
      />,
    );
    expect(screen.getByLabelText('endpoint').textContent).toBe('unknown');
    expect(container.textContent).not.toContain(raw);
  });

  it('renders unknown for null generation and qualification', () => {
    render(
      <NodesInternetPanel
        incarnation={null}
        generation={null}
        endpoint_pseudonym={null}
        lease_freshness="unknown"
        qualification={null}
      />,
    );
    expect(screen.getByLabelText('incarnation').textContent).toBe('unknown');
    expect(screen.getByLabelText('generation').textContent).toBe('unknown');
    expect(screen.getByLabelText('qualification').textContent).toBe('unknown');
    expect(screen.getByLabelText('endpoint').textContent).toBe('unknown');
  });

  it('renders no address or hostname field anywhere', () => {
    const { container } = render(
      <NodesInternetPanel
        incarnation="node-1-incarnation"
        generation={1}
        endpoint_pseudonym={'sha256:' + 'd'.repeat(64)}
        lease_freshness="current"
        qualification="qualified"
      />,
    );
    expect(container.textContent).not.toMatch(/\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/);
    expect(container.textContent).not.toContain('https://');
  });
});
