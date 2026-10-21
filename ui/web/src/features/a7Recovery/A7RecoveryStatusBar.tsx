// SPDX-License-Identifier: AGPL-3.0-only
import { useProductEvidence } from '../productEvidence/ProductEvidenceContext';
import type { ProductEntity } from '../productEvidence/contracts';
import workspaceFieldVocabulary from './workspaceFields.json';

/**
 * A7 KV-successor recovery workspaces.  The eight keys here are exactly the
 * A7 gate's `THRESHOLDS["required_workspaces"]`:
 * (inference, device_lab, network, nodes, plans, readiness, incidents,
 * settings).  The vocabulary in workspaceFields.json is the gate's frozen
 * field list; each panel renders one key's fields.
 *
 * A7-specific counters (committed tokens, ack waits, fence generations) are
 * projected by the observatory stream when available.  Fields not yet in the
 * snapshot render `unavailable` — never an invented value.
 */

export const A7_WORKSPACE_KEYS = [
  'inference',
  'device_lab',
  'network',
  'nodes',
  'plans',
  'readiness',
  'incidents',
  'settings',
] as const;

export type A7WorkspaceKey = (typeof A7_WORKSPACE_KEYS)[number];

type A7Field = readonly [label: string, value: string];

type A7Attributes = Readonly<Record<string, unknown>>;

function attributes(entity: ProductEntity | undefined): A7Attributes {
  return (entity?.attributes ?? {}) as A7Attributes;
}

function read(entity: ProductEntity | undefined, key: string): string {
  const value = attributes(entity)[key];
  if (value === undefined || value === null) return 'unavailable';
  return String(value);
}

function workspaceFields(
  workspace: A7WorkspaceKey,
  entity: ProductEntity | undefined,
): readonly A7Field[] {
  const labels = workspaceFieldVocabulary[workspace];
  switch (workspace) {
    case 'inference':
      return [
        [labels[0], read(entity, 'committed_token_count')],
        [labels[1], read(entity, 'sync_ack_wait_median_ms')],
        [labels[2], read(entity, 'recovery_prefill_operations')],
        [labels[3], read(entity, 'duplicate_token_count')],
      ];
    case 'device_lab':
      return [
        [labels[0], read(entity, 'standby_placement_fingerprint')],
        [labels[1], read(entity, 'runtime_backend')],
        [labels[2], read(entity, 'kv_reservation_bytes')],
        [labels[3], read(entity, 'measured_free_bytes')],
      ];
    case 'network':
      return [
        [labels[0], read(entity, 'last_checkpoint_bytes_per_sec')],
        [labels[1], read(entity, 'last_ack_rtt_ms')],
        [labels[2], read(entity, 'fence_advance_sequence')],
        [labels[3], read(entity, 'transport_rtt_to_standby_ms')],
      ];
    case 'nodes':
      return [
        [labels[0], read(entity, 'entry_node_binding')],
        [labels[1], read(entity, 'source_node_binding')],
        [labels[2], read(entity, 'standby_node_binding')],
        [labels[3], read(entity, 'process_exited_incident_count')],
      ];
    case 'plans':
      return [
        [labels[0], read(entity, 'standby_plan_digest')],
        [labels[1], read(entity, 'deployment_id_epoch')],
        [labels[2], read(entity, 'qualification_age_s')],
        [labels[3], read(entity, 'parity_challenge_digest')],
      ];
    case 'readiness':
      return [
        [labels[0], read(entity, 'fence_ledger_state')],
        [labels[1], read(entity, 'durable_fence_generation')],
        [labels[2], read(entity, 'parity_challenge_status')],
        [labels[3], read(entity, 'qualification_ttl_remaining_s')],
      ];
    case 'incidents':
      return [
        [labels[0], read(entity, 'scoped_incident_count_1h')],
        [labels[1], read(entity, 'incident_reason_code')],
        [labels[2], read(entity, 'recovery_path_chosen')],
        [labels[3], read(entity, 'terminal_blocked_count')],
      ];
    case 'settings':
      return [
        [labels[0], read(entity, 'collection_window_ms')],
        [labels[1], read(entity, 'command_timeout_ms')],
        [labels[2], read(entity, 'ack_timeout_s')],
        [labels[3], read(entity, 'receipt_timeout_s')],
      ];
  }
}

export function A7RecoveryStatusBar({
  workspace,
  enabled = true,
}: {
  readonly workspace: A7WorkspaceKey;
  readonly enabled?: boolean;
}) {
  const evidence = useProductEvidence();
  const visible = enabled ? evidence.visible : null;
  const entity = visible?.snapshot.entities.find(({ kind, entity_id }) => (
    kind === 'recovery' && entity_id === 'a7-kv-successor-runtime'
  ));

  return (
    <aside
      className="a7-recovery-status-bar"
      aria-label="A7 KV-successor recovery status"
      data-workspace={workspace}
    >
      <span className="eyebrow">A7 KV-successor recovery</span>
      {visible === null || entity === undefined ? (
        <span role="status">
          {evidence.loading ? 'Checking bounded status' : 'A7 recovery evidence unavailable'}
        </span>
      ) : (
        <dl>
          {workspaceFields(workspace, entity).map(([label, value]) => (
            <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
          ))}
        </dl>
      )}
    </aside>
  );
}
