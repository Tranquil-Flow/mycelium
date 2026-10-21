// SPDX-License-Identifier: AGPL-3.0-only
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ProductEvidenceProvider } from '../productEvidence/ProductEvidenceContext';
import type { ProductEvidenceState } from '../productEvidence/source';
import { decodeProductSnapshot, type ProductSnapshot } from '../productEvidence/contracts';
import productSnapshotFixture from '../../../../../contracts/compatibility-fixtures/product-snapshot-v1.json';
import {
  A7RecoveryStatusBar,
  A7_WORKSPACE_KEYS,
  type A7WorkspaceKey,
} from './A7RecoveryStatusBar';
import workspaceFieldVocabulary from './workspaceFields.json';

/**
 * The A7 gate's THRESHOLDS["required_workspaces"] is the source of truth.
 * This test pins the workspace keys to that exact list and asserts each
 * panel covers every field name in workspaceFields.json.
 */

const GATE_REQUIRED_WORKSPACES = [
  'inference',
  'device_lab',
  'network',
  'nodes',
  'plans',
  'readiness',
  'incidents',
  'settings',
] as const;

function snapshot(): ProductSnapshot {
  return {
    internet_native: decodeProductSnapshot(productSnapshotFixture).internet_native,
    protocol: 'mycelium.product_snapshot.v1',
    publication: {
      snapshot_id: 'snapshot-a7',
      generation: 1,
      cursor: 1,
      published_at_unix_ms: 1_500_000,
      source_mode: 'live',
    },
    supported_entity_kinds: [
      'artifact', 'assignment', 'device', 'directed_link', 'incident',
      'load_proof', 'qualification', 'recovery', 'request', 'route',
      'runtime_kv_ownership', 'source_provenance', 'stage',
    ],
    source_states: [{
      source_id: 'recovery-source',
      authority: 'request_gateway_recovery',
      status: 'current',
      observed_at_unix_ms: 1_500_000,
      valid_until_unix_ms: 1_500_000,
      generation: null,
      reason_code: null,
    }],
    entities: [{
      entity_id: 'a7-kv-successor-runtime',
      kind: 'recovery',
      label: 'A7 KV-successor recovery',
      source_id: 'recovery-source',
      binding: {
        deployment_id: 'deployment-a7',
        deployment_epoch: 1,
        route_id: null,
        route_generation: null,
        topology_version: null,
      },
      freshness: {
        status: 'current',
        observed_at_unix_ms: 1_500_000,
        valid_until_unix_ms: 1_500_000,
      },
      attributes: {
        committed_token_count: 4,
        sync_ack_wait_median_ms: 12,
        recovery_prefill_operations: 0,
        duplicate_token_count: 0,
      },
    }],
    relations: [],
    readiness: [],
    notices: [],
    provenance: {
      projector: 'mycelium_product_spine',
      projector_version: 'm12-v1',
      source_mode: 'live',
    },
  };
}

function state(): ProductEvidenceState {
  return {
    status: 'connected',
    source_mode: 'live',
    freshness: 'current',
    generation: 1,
    cursor: 1,
    snapshot: snapshot(),
    reason_code: null,
  };
}

function mutableSource(initial: ProductEvidenceState) {
  let current = initial;
  let listener: ((next: ProductEvidenceState) => void) | null = null;
  return {
    getState: () => current,
    loadInitial: vi.fn(async () => current),
    subscribe: vi.fn((next: (value: ProductEvidenceState) => void) => {
      listener = next;
      return () => { listener = null; };
    }),
    emit(next: ProductEvidenceState) { current = next; listener?.(next); },
  };
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('A7 KV-successor recovery workspaces', () => {
  it('pins the workspace keys to the gate-required list exactly', () => {
    expect(A7_WORKSPACE_KEYS).toEqual(GATE_REQUIRED_WORKSPACES);
  });

  it('renders every workspace with its full vocabulary from workspaceFields.json', () => {
    const source = mutableSource(state());
    for (const workspace of A7_WORKSPACE_KEYS) {
      render(
        <ProductEvidenceProvider source={source}>
          <A7RecoveryStatusBar workspace={workspace} />
        </ProductEvidenceProvider>,
      );
      const bar = screen.getByLabelText('A7 KV-successor recovery status');
      expect(bar).toHaveAttribute('data-workspace', workspace);
      for (const label of workspaceFieldVocabulary[workspace]) {
        expect(bar).toHaveTextContent(label);
      }
      cleanup();
    }
  });

  it('renders unavailable for A7 fields absent from the snapshot, never an invented value', () => {
    const source = mutableSource(state());
    render(
      <ProductEvidenceProvider source={source}>
        <A7RecoveryStatusBar workspace="readiness" />
      </ProductEvidenceProvider>,
    );
    const bar = screen.getByLabelText('A7 KV-successor recovery status');
    // 'Fence ledger state' is not in the fixture snapshot's attributes.
    expect(bar).toHaveTextContent('unavailable');
  });

});
