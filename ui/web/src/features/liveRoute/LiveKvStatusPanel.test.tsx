import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { LiveKvStatusPanel } from './LiveKvStatusPanel';
import { liveRouteStatusFixture } from './routeStatusTestFixture';

const client = { load: async () => liveRouteStatusFixture() };

describe('LiveKvStatusPanel', () => {
  it('shows the selected model and qualified decode mode during inference', async () => {
    render(<LiveKvStatusPanel view="inference" freshness="current" client={client} />);

    expect(await screen.findByText('Qwen/Qwen2.5-0.5B-Instruct')).toBeVisible();
    expect(screen.getByText('stage_local_kv live')).toBeVisible();
    expect(screen.getByText('released / idle')).toBeVisible();
    expect(screen.getByText('Released')).toBeVisible();
    expect(screen.getByText(/released its stage-local KV state/i)).toBeVisible();
  });

  it('shows architecture, capability, memory, and release evidence per node', async () => {
    render(<LiveKvStatusPanel view="nodes" freshness="current" client={client} />);

    expect(await screen.findByText('node-0')).toBeVisible();
    expect(screen.getByText('complete_context_replay, stage_local_kv')).toBeVisible();
    expect(screen.getByText(/0 active · 0 B · peak 4.0 KiB · released/)).toBeVisible();
    expect(screen.getByText(/8 input tokens \/ 8 operations/)).toBeVisible();
  });

  it('does not present stale evidence as qualified', async () => {
    render(<LiveKvStatusPanel view="inference" freshness="stale" client={client} />);

    expect(await screen.findByText('Not qualified for new work')).toBeVisible();
    expect(screen.getByText('stale')).toBeVisible();
  });
});
