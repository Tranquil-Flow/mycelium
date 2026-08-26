import { describe, expect, it } from 'vitest';
import { productSnapshotWithInternetNative } from '../internetNative/testFixtures';
import {
  ProductEvidenceContractError,
  decodeProductSnapshot,
  decodeProductSnapshotEvent,
} from './contracts';

describe('M12 product evidence contracts', () => {
  it('decodes and freezes the canonical cross-language snapshot with closed A8 evidence', () => {
    const decoded = decodeProductSnapshot(productSnapshotWithInternetNative());
    expect(decoded.protocol).toBe('mycelium.product_snapshot.v1');
    expect(decoded.publication.source_mode).toBe('fixture');
    expect(decoded.entities[0].attributes.peer_class).toBe('android_termux_iroh');
    expect(decoded.internet_native.bootstrap_status.protocol).toBe(
      'mycelium.internet_bootstrap_status.v1',
    );
    expect(decoded.internet_native.activation_history).toHaveLength(1);
    expect(decoded.internet_native.relay_projection?.relay_reference).toMatch(/^hmac-sha256:/);
    expect(decoded.internet_native.qualification?.result).toBe('not_executed');
    expect(Object.isFrozen(decoded.entities[0].attributes)).toBe(true);
    expect(Object.isFrozen(decoded.internet_native.activation_observation.metrics)).toBe(true);
  });

  it('rejects unknown fields, unknown major protocols, and private payload lanes', () => {
    const snapshot = productSnapshotWithInternetNative();
    expect(() => decodeProductSnapshot({ ...snapshot, prompt: 'private' })).toThrow(
      ProductEvidenceContractError,
    );
    expect(() => decodeProductSnapshot({
      ...snapshot,
      protocol: 'mycelium.product_snapshot.v2',
    })).toThrow(ProductEvidenceContractError);
    const privateLane = structuredClone(snapshot) as Record<string, unknown> & {
      internet_native: Record<string, unknown>;
    };
    privateLane.internet_native.token = 'private';
    expect(() => decodeProductSnapshot(privateLane)).toThrow(ProductEvidenceContractError);
  });

  it('requires all five exact internet_native fields and rejects raw relay identity', () => {
    const missingHistory = productSnapshotWithInternetNative();
    const internetNative = missingHistory.internet_native as Record<string, unknown>;
    delete internetNative.activation_history;
    expect(() => decodeProductSnapshot(missingHistory)).toThrow(ProductEvidenceContractError);

    const rawRelay = productSnapshotWithInternetNative();
    const relay = (rawRelay.internet_native as {
      relay_projection: Record<string, unknown>;
    }).relay_projection;
    relay.relay_reference = 'https://relay.example:443';
    expect(() => decodeProductSnapshot(rawRelay)).toThrow(ProductEvidenceContractError);
  });

  it('requires a contiguous event cursor bound to the complete snapshot without changing product_event.v1', () => {
    const event = {
      protocol: 'mycelium.product_event.v1',
      cursor: 1,
      previous_cursor: 0,
      event_kind: 'snapshot_published',
      snapshot: productSnapshotWithInternetNative(),
    };
    const decoded = decodeProductSnapshotEvent(event);
    expect(decoded.protocol).toBe('mycelium.product_event.v1');
    expect(decoded.cursor).toBe(1);
    expect(decoded.snapshot.internet_native.activation_observation.observation_id).toBe(
      'fixture-observation',
    );
    expect(() => decodeProductSnapshotEvent({ ...event, cursor: 2 })).toThrow(
      ProductEvidenceContractError,
    );
  });
});
