import { describe, expect, it } from 'vitest';
import { validSemanticEvent, validSemanticSnapshot } from '../test/semanticFixture';
import {
  OBSERVATORY_EVENT_PROTOCOL,
  OBSERVATORY_SNAPSHOT_PROTOCOL,
  UnsupportedObservatoryProtocolError,
  decodeObservatoryEvent,
  decodeObservatorySnapshot,
  parseObservatoryEventHeader,
  qualifySemanticSnapshot,
} from './semanticProjection';

const NOW = Date.parse('2026-07-18T12:00:00Z');
const CANARY = 'PHASE9_PRIVACY_CANARY_DO_NOT_PUBLISH';

function clone<T>(value: T): T {
  return structuredClone(value);
}

describe('semantic Observatory v1 contracts', () => {
  it('strictly decodes exact snapshot and event v1 projections', () => {
    const snapshotInput = validSemanticSnapshot();
    const snapshot = decodeObservatorySnapshot(snapshotInput);
    const event = decodeObservatoryEvent(validSemanticEvent(9));

    snapshotInput.binding.deployment.id = 'mutated';
    expect(snapshot.protocol).toBe(OBSERVATORY_SNAPSHOT_PROTOCOL);
    expect(snapshot.binding.deployment.id).toBe('deployment-alpha');
    expect(event.protocol).toBe(OBSERVATORY_EVENT_PROTOCOL);
    expect(event.generation).toBe(9);
    expect(event.snapshot).toEqual(snapshot);
    expect(Object.isFrozen(event.snapshot)).toBe(true);
  });

  it('parses generation before deep decoding so stale malformed payloads can be ignored', () => {
    const event = validSemanticEvent(12);
    event.snapshot = { malformed: true } as never;

    expect(parseObservatoryEventHeader(event)).toMatchObject({
      protocol: OBSERVATORY_EVENT_PROTOCOL,
      generation: 12,
    });
    expect(() => decodeObservatoryEvent(event)).toThrow(/snapshot/i);
  });

  it.each([
    'mycelium.observatory.snapshot.v2',
    'mycelium.observatory.snapshot.v10',
  ])('fails closed on unknown snapshot major %s', (protocol) => {
    const snapshot = validSemanticSnapshot();
    snapshot.protocol = protocol;
    expect(() => decodeObservatorySnapshot(snapshot)).toThrow(UnsupportedObservatoryProtocolError);
  });

  it.each(['mycelium.observatory.event.v2', 'mycelium.observatory.event.v10'])(
    'fails closed on unknown event major %s',
    (protocol) => {
      const event = validSemanticEvent();
      event.protocol = protocol;
      expect(() => decodeObservatoryEvent(event)).toThrow(UnsupportedObservatoryProtocolError);
    },
  );

  it.each([
    'prompt',
    'token_ids',
    'token_content',
    'activations',
    'tensor',
    'weights',
    'credentials',
    'raw_endpoint',
    'raw_router_frame',
  ])('rejects forbidden or unknown field %s without retaining its canary', (field) => {
    const snapshot = validSemanticSnapshot() as Record<string, unknown>;
    snapshot[field] = CANARY;
    let message = '';
    try {
      decodeObservatorySnapshot(snapshot);
    } catch (reason) {
      message = reason instanceof Error ? reason.message : String(reason);
    }
    expect(message).toMatch(/unknown|field/i);
    expect(message).not.toContain(CANARY);
  });

  it.each(['127.0.0.1', '2001:db8::1', 'https://peer.invalid', '/private/socket'])(
    'rejects raw endpoint, IP, or path identifier %s',
    (peerId) => {
      const snapshot = validSemanticSnapshot();
      snapshot.binding.route.assignments[0].peer_id = peerId;
      snapshot.route_challenge.binding = clone(snapshot.binding);
      snapshot.request_lifecycle.binding = clone(snapshot.binding);
      expect(() => decodeObservatorySnapshot(snapshot)).toThrow(/endpoint|path|IP/i);
    },
  );

  it('requires exact deployment/model/route/assignment binding in challenge and lifecycle', () => {
    const lifecycleMismatch = validSemanticSnapshot();
    lifecycleMismatch.request_lifecycle.binding.model.revision = 'commit-other';
    expect(() => decodeObservatorySnapshot(lifecycleMismatch)).toThrow(/exactly match/i);

    const challengeMismatch = validSemanticSnapshot();
    challengeMismatch.route_challenge.binding.route.assignments[0].end_layer_exclusive = 1;
    expect(() => decodeObservatorySnapshot(challengeMismatch)).toThrow(
      /coverage|exactly match/i,
    );

    const claimProvenanceMismatch = validSemanticSnapshot();
    claimProvenanceMismatch.claims.at(-1)!.provenance = {
      kind: 'gateway_projection',
      producer: 'mycelium_gateway',
    };
    expect(() => decodeObservatorySnapshot(claimProvenanceMismatch)).toThrow(/provenance/i);
  });

  it('preserves only exact explicit conflicts and refuses live qualification', () => {
    const crossScope = validSemanticSnapshot();
    crossScope.conflicts = [
      {
        claim_ids: ['claim-challenge', 'claim-request'],
        scope: { kind: 'route', id: 'route-primary' },
        reason: 'binding_mismatch',
      },
    ];
    expect(() => decodeObservatorySnapshot(crossScope)).toThrow(/conflict scope/i);

    const snapshot = validSemanticSnapshot();
    const duplicateRouteClaim = clone(snapshot.claims[4]);
    duplicateRouteClaim.id = 'claim-challenge-disagreeing';
    duplicateRouteClaim.value = 'rejected';
    snapshot.claims.push(duplicateRouteClaim);
    snapshot.conflicts = [
      {
        claim_ids: ['claim-challenge', 'claim-challenge-disagreeing'],
        scope: { kind: 'route', id: 'route-primary' },
        reason: 'value_mismatch',
      },
    ];
    const decoded = decodeObservatorySnapshot(snapshot);
    const qualification = qualifySemanticSnapshot(decoded, NOW);

    expect(decoded.conflicts).toHaveLength(1);
    expect(qualification.live).toBe(false);
    expect(qualification.reasons).toContain('conflicts_present');

    const unreported = validSemanticSnapshot();
    unreported.claims.push(duplicateRouteClaim);
    expect(() => decodeObservatorySnapshot(unreported)).toThrow(/exact conflict/i);
  });

  it('requires fresh successful route challenge and completed real request lifecycle', () => {
    expect(qualifySemanticSnapshot(decodeObservatorySnapshot(validSemanticSnapshot()), NOW)).toEqual({
      live: true,
      reasons: [],
    });

    const failedChallenge = validSemanticSnapshot();
    failedChallenge.route_challenge.status = 'failed';
    expect(
      qualifySemanticSnapshot(decodeObservatorySnapshot(failedChallenge), NOW).reasons,
    ).toContain('route_challenge_not_successful');

    const failedRequest = validSemanticSnapshot();
    failedRequest.request_lifecycle.state = 'failed';
    expect(qualifySemanticSnapshot(decodeObservatorySnapshot(failedRequest), NOW).reasons).toContain(
      'request_lifecycle_not_completed',
    );

    const stale = validSemanticSnapshot();
    stale.freshness = {
      observed_at: '2026-07-18T11:00:00Z',
      valid_until: '2026-07-18T11:30:00Z',
    };
    stale.route_challenge.freshness = clone(stale.freshness);
    stale.request_lifecycle.freshness = clone(stale.freshness);
    for (const claim of stale.claims) claim.freshness = clone(stale.freshness);
    expect(qualifySemanticSnapshot(decodeObservatorySnapshot(stale), NOW).reasons).toContain(
      'snapshot_stale',
    );
  });
});
