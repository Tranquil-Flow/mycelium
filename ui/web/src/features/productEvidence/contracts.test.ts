import { describe, expect, it } from 'vitest';
import fixture from '../../../../../contracts/compatibility-fixtures/product-snapshot-v1.json';
import {
  ProductEvidenceContractError,
  decodeProductSnapshot,
  decodeProductSnapshotEvent,
} from './contracts';

describe('M12 product evidence contracts', () => {
  it('decodes and freezes the canonical cross-language snapshot', () => {
    const decoded = decodeProductSnapshot(structuredClone(fixture));
    expect(decoded.protocol).toBe('mycelium.product_snapshot.v1');
    expect(decoded.publication.source_mode).toBe('fixture');
    expect(decoded.entities[0].attributes.peer_class).toBe('android_termux_iroh');
    expect(Object.isFrozen(decoded.entities[0].attributes)).toBe(true);
  });

  it('rejects unknown fields, unknown major protocols, and private payload lanes', () => {
    expect(() => decodeProductSnapshot({ ...fixture, prompt: 'private' })).toThrow(
      ProductEvidenceContractError,
    );
    expect(() => decodeProductSnapshot({ ...fixture, protocol: 'mycelium.product_snapshot.v2' })).toThrow(
      ProductEvidenceContractError,
    );
    const privateLane = structuredClone(fixture) as typeof fixture & {
      entities: Array<(typeof fixture.entities)[number] & { attributes: Record<string, unknown> }>;
    };
    privateLane.entities[0].attributes.token_ids = [1, 2, 3];
    expect(() => decodeProductSnapshot(privateLane)).toThrow(ProductEvidenceContractError);
  });

  it('requires a contiguous event cursor bound to the complete snapshot', () => {
    const event = {
      protocol: 'mycelium.product_event.v1',
      cursor: 1,
      previous_cursor: 0,
      event_kind: 'snapshot_published',
      snapshot: fixture,
    };
    expect(decodeProductSnapshotEvent(event).cursor).toBe(1);
    expect(() => decodeProductSnapshotEvent({ ...event, cursor: 2 })).toThrow(
      ProductEvidenceContractError,
    );
  });
});
