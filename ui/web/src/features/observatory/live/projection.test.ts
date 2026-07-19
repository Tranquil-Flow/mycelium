import { describe, expect, it } from 'vitest';
import { decodeObservatoryAdapterEvent } from '../../../data/observatoryEventProjection';
import { loadStaticObservatoryBundle } from '../../../data/observatorySource';
import { validObservatoryAdapterEvent } from '../../../test/observatoryEventFixture';
import {
  projectFixtureObservatoryBundle,
  projectLiveObservatoryBundle,
} from './projection';

const PRIVATE_CANARY = 'OBSERVATORY_PRODUCT_PROJECTION_PRIVATE_CANARY';

describe('product Observatory projection', () => {
  it('normalizes fixture entities without relabeling fixture evidence or readiness', () => {
    const projection = projectFixtureObservatoryBundle(loadStaticObservatoryBundle());

    expect(projection.source_kind).toBe('fixture');
    expect(projection.route_ready).toBe(false);
    expect(projection.observed_at_unix_ms).toBeNull();
    expect(projection.entities.nodes.length).toBeGreaterThan(0);
    expect(projection.metrics).toEqual({
      native_node_count: projection.entities.nodes.length,
      browser_worker_count: null,
      incident_count: projection.source.incidents.length,
    });
    expect(projection.entities.nodes[0].revision).toMatch(/^v1:[0-9a-f]{32}$/);
    expect(Object.isFrozen(projection.entities.nodes)).toBe(true);
    expect(Object.isFrozen(projection)).toBe(true);
  });

  it('preserves literal unknown protocol values and literal route_ready from strict live data', () => {
    const input = validObservatoryAdapterEvent(4, 9);
    input.bundle.incidents[0].protocol = 'unknown';
    const decoded = decodeObservatoryAdapterEvent(input);
    const projection = projectLiveObservatoryBundle(decoded.bundle);

    expect(projection.source_kind).toBe('event_adapter');
    expect(projection.route_ready).toBe(decoded.bundle.provisioning.route_ready);
    expect(projection.metrics.native_node_count).toBeNull();
    expect(projection.entities.evidence).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: 'incident~2~0',
          value: expect.objectContaining({ protocol: 'unknown' }),
        }),
      ]),
    );
  });

  it('uses deterministic privacy-reduced revisions and never copies rejected private fields', () => {
    const first = decodeObservatoryAdapterEvent(validObservatoryAdapterEvent(1, 3));
    const second = decodeObservatoryAdapterEvent(validObservatoryAdapterEvent(2, 4));
    const firstProjection = projectLiveObservatoryBundle(first.bundle);
    const sameProjection = projectLiveObservatoryBundle(first.bundle);
    const nextProjection = projectLiveObservatoryBundle(second.bundle);

    expect(firstProjection.entities.evidence[0].revision).toBe(
      sameProjection.entities.evidence[0].revision,
    );
    expect(firstProjection.entities.readiness[0].revision).not.toBe(
      nextProjection.entities.readiness[0].revision,
    );

    const poisoned = structuredClone(first.bundle) as typeof first.bundle & { prompt?: string };
    poisoned.prompt = PRIVATE_CANARY;
    expect(() =>
      decodeObservatoryAdapterEvent({
        protocol: 'mycelium.observatory_stream.v1',
        generation: 3,
        bundle: poisoned,
      }),
    ).toThrow(/unknown|fields/i);
    expect(JSON.stringify(firstProjection)).not.toContain(PRIVATE_CANARY);
  });
});
