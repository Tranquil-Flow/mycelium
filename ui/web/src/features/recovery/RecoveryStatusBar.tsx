// SPDX-License-Identifier: AGPL-3.0-only
import type { ProductRouteId } from '../../app/navigation';
import { useProductEvidence } from '../productEvidence/ProductEvidenceContext';
import type { ProductEntity } from '../productEvidence/contracts';
import workspaceFieldVocabulary from './workspaceFields.json';

type RecoveryField = readonly [label: string, value: string];

type RecoveryAttributes = Readonly<{
  request_count: number;
  recovering_count: number;
  terminal_count: number;
  terminal_blocked_count: number;
  cleanup_complete_count: number;
  reconciled_count: number;
  max_recovery_attempt: number;
  breaker_state: string;
}>;

function attributes(entity: ProductEntity): RecoveryAttributes {
  return entity.attributes as RecoveryAttributes;
}

function workspaceFields(
  route: ProductRouteId,
  recovery: RecoveryAttributes,
  sourceState: string,
  historyCount: number,
  cursor: number,
): readonly RecoveryField[] {
  const labels = workspaceFieldVocabulary[route];
  switch (route) {
    case 'inference':
      return [
        [labels[0], String(recovery.request_count)],
        [labels[1], String(recovery.recovering_count)],
        [labels[2], String(recovery.terminal_count)],
      ];
    case 'lab':
      return [
        [labels[0], String(recovery.max_recovery_attempt)],
        [labels[1], String(recovery.cleanup_complete_count)],
        [labels[2], recovery.breaker_state],
      ];
    case 'network':
      return [
        [labels[0], sourceState],
        [labels[1], String(cursor)],
        [labels[2], String(recovery.recovering_count)],
      ];
    case 'nodes':
      return [
        [labels[0], String(recovery.request_count)],
        [labels[1], String(recovery.cleanup_complete_count)],
        [labels[2], String(recovery.reconciled_count)],
      ];
    case 'plans':
      return [
        [labels[0], String(recovery.max_recovery_attempt)],
        [labels[1], String(recovery.recovering_count)],
        [labels[2], String(recovery.terminal_blocked_count)],
      ];
    case 'readiness':
      return [
        [labels[0], sourceState],
        [labels[1], String(recovery.cleanup_complete_count)],
        [labels[2], String(recovery.reconciled_count)],
      ];
    case 'incidents':
      return [
        [labels[0], String(recovery.terminal_count)],
        [labels[1], String(recovery.terminal_blocked_count)],
        [labels[2], recovery.breaker_state],
      ];
    case 'settings':
      return [
        [labels[0], String(recovery.max_recovery_attempt)],
        [labels[1], sourceState],
        [labels[2], String(historyCount)],
      ];
  }
}

export function RecoveryStatusBar({
  route,
  enabled = true,
}: {
  readonly route: ProductRouteId;
  readonly enabled?: boolean;
}) {
  const evidence = useProductEvidence();
  const visible = enabled ? evidence.visible : null;
  const source = visible?.snapshot.source_states.find(({ source_id }) => source_id === 'recovery-source');
  const entity = visible?.snapshot.entities.find(({ kind, entity_id }) => (
    kind === 'recovery' && entity_id === 'recovery-runtime'
  ));

  return (
    <aside className="recovery-status-bar" aria-label="Full-context replay recovery status" data-route={route}>
      <span className="eyebrow">Full-context replay recovery</span>
      {visible === null || entity === undefined ? (
        <span role="status">
          {evidence.loading ? 'Checking bounded status' : 'Shared recovery evidence unavailable'}
        </span>
      ) : (
        <>
          <span>
            Shared cursor {visible.cursor} · Shared history {evidence.history.length}
          </span>
          <dl>
            {workspaceFields(
              route,
              attributes(entity),
              source?.status ?? 'missing',
              evidence.history.length,
              visible.cursor,
            ).map(([label, value]) => (
              <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
            ))}
          </dl>
        </>
      )}
    </aside>
  );
}
