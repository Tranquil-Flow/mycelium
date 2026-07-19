import { describe, expect, it } from 'vitest';
import {
  decodeProductBootstrap,
  decodeProductObservatory,
  decodeProductQualification,
  decodeProductSwarmStatus,
} from '../app/contracts';
import {
  ProductFixturePrivacyError,
  assertProductFixturePrivacy,
  makeAcceptedQualificationContractFixture,
  makeProductBootstrapFixture,
  makeProductObservatoryFixture,
  makeProductQualificationFixture,
  makeProductSwarmFixture,
} from './productFixtures';

describe('product fixture factories', () => {
  it('builds strict fixtures accepted by every product decoder', () => {
    const bootstrap = decodeProductBootstrap(makeProductBootstrapFixture());
    const observatory = decodeProductObservatory(makeProductObservatoryFixture());
    const qualification = decodeProductQualification(makeProductQualificationFixture());
    const swarm = decodeProductSwarmStatus(makeProductSwarmFixture());

    expect(JSON.stringify(bootstrap)).not.toContain('route_ready');
    expect(qualification.route_ready).toBe(false);
    expect(JSON.stringify(observatory)).not.toContain('route_ready');
    expect(observatory.metrics.native_node_count).toBeNull();
    expect(JSON.stringify(swarm)).not.toContain('route_ready');
    expect(swarm.native_nodes[0]).not.toHaveProperty('private_address');
  });

  it('keeps accepted physical evidence explicit and contract-test-only', () => {
    const accepted = decodeProductQualification(makeAcceptedQualificationContractFixture());
    expect(accepted.route_ready).toBe(true);
    expect(accepted.evidence_class).toBe('physical_qualification');
  });

  it('returns independent mutable roots so tests cannot contaminate each other', () => {
    const first = makeProductObservatoryFixture();
    const second = makeProductObservatoryFixture();
    expect(first).not.toBe(second);
    expect(first.metrics).not.toBe(second.metrics);
    expect(first.source).not.toBe(second.source);
  });

  it('rejects private payload and network identity fields recursively', () => {
    expect(() =>
      assertProductFixturePrivacy({ telemetry: { prompt: 'private words' } }),
    ).toThrow(ProductFixturePrivacyError);
    expect(() =>
      assertProductFixturePrivacy({ node: { ip_address: '10.0.0.4' } }),
    ).toThrow(/private network identity prohibited/i);
    expect(() =>
      assertProductFixturePrivacy({ nested: [{ token_ids: [1, 2] }] }),
    ).toThrow(/private payload field prohibited/i);
    expect(() =>
      assertProductFixturePrivacy({ auth: { authorization: 'Bearer secret' } }),
    ).toThrow(ProductFixturePrivacyError);
    expect(() =>
      assertProductFixturePrivacy({ target: { endpoint_url: 'http://10.0.0.4:9000' } }),
    ).toThrow(ProductFixturePrivacyError);
    for (const alias of ['apiKey', 'accessToken', 'clientSecret', 'cookie', 'sessionToken']) {
      expect(() => assertProductFixturePrivacy({ [alias]: 'secret' })).toThrow(
        ProductFixturePrivacyError,
      );
    }
    const hidden = [{ safe: true }] as Array<unknown> & { forEach: () => void };
    hidden.forEach = () => undefined;
    expect(() => assertProductFixturePrivacy(hidden)).toThrow(/extended fixture array/i);
    const nonEnumerable: Record<string, unknown> = { safe: true };
    Object.defineProperty(nonEnumerable, 'authorization', {
      enumerable: false,
      value: 'Bearer secret',
    });
    expect(() => assertProductFixturePrivacy(nonEnumerable)).toThrow(/hidden/i);
    expect(() => assertProductFixturePrivacy({ [Symbol('secret')]: 'x' })).toThrow(/symbol/i);
    expect(() =>
      assertProductFixturePrivacy(Object.create({ inherited_secret: 'x' })),
    ).toThrow(/plain objects/i);
  });

  it('permits only the narrow same-origin CSRF capability fixture', () => {
    expect(() => assertProductFixturePrivacy(makeProductBootstrapFixture())).not.toThrow();
    expect(() => assertProductFixturePrivacy({ auth_token: 'secret' })).toThrow(
      ProductFixturePrivacyError,
    );
    expect(() => assertProductFixturePrivacy({ csrf_token: 'mis-scoped' })).toThrow(
      ProductFixturePrivacyError,
    );
  });

  it('rejects cycles rather than recursing forever', () => {
    const cycle: { self?: unknown } = {};
    cycle.self = cycle;
    expect(() => assertProductFixturePrivacy(cycle)).toThrow(/cyclic fixture object/i);
  });
});
