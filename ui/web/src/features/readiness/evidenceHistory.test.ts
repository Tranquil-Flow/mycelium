import { describe, expect, it } from 'vitest';
import { loadStaticObservatoryBundle } from '../../data/observatorySource';
import {
  buildEvidenceTimeline,
  diffEvidenceFrames,
  evidenceSources,
} from './evidenceHistory';

describe('evidence source history', () => {
  const bundle = loadStaticObservatoryBundle();

  it('records protocol, redacted locator, digest state, times, validation, and claim boundary', () => {
    const sources = evidenceSources(bundle.snapshot, bundle.provisioning, bundle.incidents);

    expect(sources.length).toBeGreaterThan(4);
    expect(sources.every((source) => source.protocol.length > 0)).toBe(true);
    expect(sources.every((source) => source.locator.startsWith('bundled://'))).toBe(true);
    expect(sources.some((source) => source.rawDigest.state === 'unknown')).toBe(true);
    expect(sources.every((source) => source.claimBoundary.length > 0)).toBe(true);
    expect(sources.every((source) => source.validation.state === 'VALIDATED')).toBe(true);
  });

  it('builds an immutable replay timeline only from supplied timestamps and transitions', () => {
    const timeline = buildEvidenceTimeline(bundle.snapshot, bundle.provisioning, bundle.incidents);

    expect(timeline.length).toBeGreaterThan(bundle.incidents.length);
    expect(timeline.every((frame, index) => index === 0 || frame.atMs >= timeline[index - 1].atMs)).toBe(true);
    expect(timeline.some((frame) => frame.kind === 'incident_transition')).toBe(true);
    expect(timeline.every((frame) => frame.evidenceRef.length > 0)).toBe(true);
    expect(Object.isFrozen(timeline)).toBe(true);
  });

  it('diffs supplied comparable frames and refuses to invent a baseline', () => {
    const noBaseline = diffEvidenceFrames(null, {
      id: 'g1',
      values: { route_ready: 'NOT_PROVEN' },
    });
    expect(noBaseline.state).toBe('not_comparable');

    const diff = diffEvidenceFrames(
      { id: 'g1', values: { route_ready: 'NOT_PROVEN', artifacts: 'PROVEN' } },
      { id: 'g2', values: { route_ready: 'PROVEN', artifacts: 'PROVEN' } },
    );
    expect(diff.state).toBe('compared');
    expect(diff.changes).toEqual([
      { field: 'route_ready', before: 'NOT_PROVEN', after: 'PROVEN' },
    ]);
  });
});
