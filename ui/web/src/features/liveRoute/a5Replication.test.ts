import { describe, expect, it } from 'vitest';
import {
  decodeA5ReplicaTrackQualification,
  decodeA5ReplicaTrackQualifications,
} from './a5Replication';
import { a5QualificationFixture } from './routeStatusTestFixture';

describe('decodeA5ReplicaTrackQualification', () => {
  it('decodes a closed replica qualification document', () => {
    const decoded = decodeA5ReplicaTrackQualification(a5QualificationFixture());
    expect(decoded.protocol).toBe('mycelium.replica_qualification.v1');
    expect(decoded.route_ready).toBe(true);
    expect(decoded.placement_id).toBe('placement-fixture-replica');
    expect(decoded.placement_ids).toEqual([
      'placement-fixture-replica',
      'placement-fixture-stage-1',
    ]);
    expect(decoded.rejected_reasons).toEqual([]);
  });

  it('rejects unknown fields', () => {
    expect(() =>
      decodeA5ReplicaTrackQualification(
        a5QualificationFixture({ extra_field: true }),
      ),
    ).toThrow(/unknown or missing fields/);
  });

  it('rejects missing fields', () => {
    const document = a5QualificationFixture();
    delete (document as Record<string, unknown>).track_id;
    expect(() => decodeA5ReplicaTrackQualification(document)).toThrow(
      /unknown or missing fields/,
    );
  });

  it('rejects a qualification that expires before issuance', () => {
    expect(() =>
      decodeA5ReplicaTrackQualification(
        a5QualificationFixture({ expires_at_unix_ms: 1 }),
      ),
    ).toThrow(/expires before issuance/);
  });

  it('rejects a non-replica protocol', () => {
    expect(() =>
      decodeA5ReplicaTrackQualification(
        a5QualificationFixture({ protocol: 'mycelium.other.v1' }),
      ),
    ).toThrow(/protocol is invalid/);
  });

  it('rejects non-boolean verification flags', () => {
    expect(() =>
      decodeA5ReplicaTrackQualification(
        a5QualificationFixture({ parity_verified: 'yes' }),
      ),
    ).toThrow(/must be a boolean/);
  });

  it.each([
    [['placement-fixture-replica']],
    [['placement-fixture-replica', 'placement-fixture-replica']],
    [['placement-other', 'placement-fixture-stage-1']],
  ])('rejects invalid complete placement membership %j', (placement_ids) => {
    expect(() =>
      decodeA5ReplicaTrackQualification(
        a5QualificationFixture({ placement_ids }),
      ),
    ).toThrow(/placement_ids/);
  });

  it('rejects malformed digest values', () => {
    expect(() =>
      decodeA5ReplicaTrackQualification(
        a5QualificationFixture({ qualification_digest: 'sha256:not-a-digest' }),
      ),
    ).toThrow(/qualification_digest/);
  });

  it('rejects identifiers exceeding the producer byte bound', () => {
    expect(() =>
      decodeA5ReplicaTrackQualification(
        a5QualificationFixture({ track_id: 'é'.repeat(129) }),
      ),
    ).toThrow(/track_id/);
  });
});

describe('decodeA5ReplicaTrackQualifications', () => {
  it('decodes a bounded list', () => {
    const decoded = decodeA5ReplicaTrackQualifications([
      a5QualificationFixture(),
      a5QualificationFixture({
        placement_id: 'p-other',
        placement_ids: ['p-other', 'placement-fixture-stage-1'],
      }),
    ]);
    expect(decoded).toHaveLength(2);
  });

  it('rejects non-array input', () => {
    expect(() => decodeA5ReplicaTrackQualifications({})).toThrow(
      /bounded array/,
    );
  });
});
