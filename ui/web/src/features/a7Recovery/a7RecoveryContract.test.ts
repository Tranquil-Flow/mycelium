// SPDX-License-Identifier: AGPL-3.0-only
import { describe, expect, it } from 'vitest';
import productSnapshotFixture from '../../../../../contracts/compatibility-fixtures/product-snapshot-v1.json';
import { A7_RECOVERY_ENTITY_ID, decodeProductSnapshot } from '../productEvidence/contracts';
import workspaceFieldVocabulary from './workspaceFields.json';

// Mirrors mycelium_product_spine.contracts.A7_RECOVERY_ATTRIBUTE_TYPES (one value per type).
const MEASURED: Record<string, unknown> = {
  committed_token_count: 2,
  sync_ack_wait_median_ms: 9.1,
  recovery_prefill_operations: 0,
  duplicate_token_count: 0,
  standby_placement_fingerprint: `sha256:${'a'.repeat(64)}`,
  runtime_backend: 'mlx',
  kv_reservation_bytes: 1048576,
  measured_free_bytes: 2147483648,
  last_checkpoint_bytes_per_sec: 120000.5,
  last_ack_rtt_ms: 9.7,
  fence_advance_sequence: 4,
  transport_rtt_to_standby_ms: 1.2,
  entry_node_binding: 'device-0123456789abcdef0123',
  source_node_binding: 'device-abcdef0123456789abcd',
  standby_node_binding: 'device-0123456789abcdef0123',
  process_exited_incident_count: 1,
  standby_plan_digest: `sha256:${'b'.repeat(64)}`,
  deployment_id_epoch: 'deployment-a7:1',
  qualification_age_s: 12.5,
  parity_challenge_digest: `sha256:${'c'.repeat(64)}`,
  fence_ledger_state: 'empty',
  durable_fence_generation: 2,
  parity_challenge_status: 'passed',
  qualification_ttl_remaining_s: -3.5,
  scoped_incident_count_1h: 2,
  incident_reason_code: 'node_process_exited',
  recovery_path_chosen: 'fenced_kv_successor',
  terminal_blocked_count: 0,
  collection_window_ms: 0,
  command_timeout_ms: 900000,
  ack_timeout_s: 2,
  receipt_timeout_s: 5,
};

function withA7(attributes: Record<string, unknown>): unknown {
  const base = JSON.parse(JSON.stringify(productSnapshotFixture)) as Record<string, unknown[]>;
  base.source_states = [
    ...base.source_states,
    {
      source_id: 'a7-source',
      authority: 'a7_entry_signed_evidence',
      status: 'current',
      observed_at_unix_ms: 1,
      valid_until_unix_ms: 1,
      generation: null,
      reason_code: null,
    },
  ];
  base.entities = [
    ...base.entities,
    {
      entity_id: A7_RECOVERY_ENTITY_ID,
      kind: 'recovery',
      label: 'Fenced KV-successor recovery',
      source_id: 'a7-source',
      binding: { deployment_id: 'deployment-a7', deployment_epoch: 1, route_id: null, route_generation: null, topology_version: null },
      freshness: { status: 'current', observed_at_unix_ms: 1, valid_until_unix_ms: 1 },
      attributes,
    },
  ];
  return base;
}

describe('A7 recovery evidence contract', () => {
  it('accepts measured values and explicit unavailable reasons', () => {
    const decoded = decodeProductSnapshot(withA7({ ...MEASURED, unavailable_reasons: {} }));
    const entity = decoded.entities.find(({ entity_id }) => entity_id === A7_RECOVERY_ENTITY_ID);
    expect(entity?.attributes.recovery_path_chosen).toBe('fenced_kv_successor');
    const unobserved = { ...MEASURED, transport_rtt_to_standby_ms: null, unavailable_reasons: { transport_rtt_to_standby_ms: 'no_standby_cutover_exchange' } };
    expect(() => decodeProductSnapshot(withA7(unobserved))).not.toThrow();
  });

  it('refuses a null without a reason, a reason beside a value, and unknown fields', () => {
    expect(() => decodeProductSnapshot(withA7({ ...MEASURED, last_ack_rtt_ms: null, unavailable_reasons: {} }))).toThrow();
    expect(() => decodeProductSnapshot(withA7({ ...MEASURED, unavailable_reasons: { last_ack_rtt_ms: 'x' } }))).toThrow();
    expect(() => decodeProductSnapshot(withA7({ ...MEASURED, extra: 1, unavailable_reasons: {} }))).toThrow();
    expect(() => decodeProductSnapshot(withA7({ ...MEASURED, recovery_path_chosen: 'maybe', unavailable_reasons: {} }))).toThrow();
  });

  it('carries one attribute per workspace field', () => {
    const fields = Object.values(workspaceFieldVocabulary).flat();
    expect(Object.keys(MEASURED)).toHaveLength(fields.length);
  });
});
