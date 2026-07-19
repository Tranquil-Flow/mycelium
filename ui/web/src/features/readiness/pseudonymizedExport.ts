import type {
  EvidenceSnapshot,
  FailoverIncident,
  ProvisioningEvidence,
} from '../../model/types';
import { buildReadinessModel } from './readinessModel';

function aliases(values: readonly string[], prefix: string): ReadonlyMap<string, string> {
  const result = new Map<string, string>();
  [...new Set(values)].sort().forEach((value, index) => {
    result.set(value, `${prefix}-${String(index + 1).padStart(3, '0')}`);
  });
  return result;
}

function requiredAlias(mapping: ReadonlyMap<string, string>, value: string): string {
  const alias = mapping.get(value);
  if (alias === undefined) throw new TypeError('Pseudonym map is incomplete');
  return alias;
}

export function createPseudonymizedExport(
  snapshot: EvidenceSnapshot,
  provisioning: ProvisioningEvidence,
  incidents: readonly FailoverIncident[],
) {
  const allNodeIds = [
    ...snapshot.nodes.map((node) => node.id),
    ...provisioning.nodeIds,
    ...incidents.flatMap((incident) => [
      ...incident.oldRoute.nodeIds,
      ...(incident.newRoute?.nodeIds ?? []),
    ]),
  ];
  const nodeAliases = aliases(allNodeIds, 'node');
  const routeAliases = aliases(
    [
      ...snapshot.routes.map((route) => route.id),
      ...incidents.flatMap((incident) => [
        incident.oldRoute.id,
        ...(incident.newRoute === null ? [] : [incident.newRoute.id]),
      ]),
    ],
    'route',
  );
  const requestAliases = aliases(
    incidents.flatMap((incident) => [...incident.requestIds]),
    'request',
  );
  const deploymentAliases = aliases(
    incidents.map((incident) => incident.deploymentId),
    'deployment',
  );
  const readiness = buildReadinessModel(provisioning);

  return Object.freeze({
    protocol: 'mycelium.ui_pseudonymized_export.v1' as const,
    source_mode: snapshot.evidenceState,
    provenance: snapshot.provenance,
    export_basis_timestamp: snapshot.source.generatedAt,
    claim_boundary: snapshot.claimBoundary,
    model: Object.freeze({
      alias: 'model-001',
      layers: snapshot.model.numLayers,
      source_identity_redacted: true,
    }),
    nodes: Object.freeze(
      [...new Set(allNodeIds)].sort().map((id) => {
        const sourceNode = snapshot.nodes.find((node) => node.id === id);
        return Object.freeze({
          alias: requiredAlias(nodeAliases, id),
          source_scope:
            sourceNode === undefined ? 'artifact_provisioning' : 'simulation',
          location:
            sourceNode?.location.state === 'known'
              ? Object.freeze({
                  country: sourceNode.location.country,
                  precision: sourceNode.location.precision,
                  coordinates_omitted: true,
                })
              : Object.freeze({ state: 'unknown' }),
        });
      }),
    ),
    plans: Object.freeze(
      snapshot.routes.map((route) =>
        Object.freeze({
          alias: requiredAlias(routeAliases, route.id),
          strategy_class: route.simulatorStrategy,
          node_order: Object.freeze(
            route.nodeOrder.map((nodeId) => requiredAlias(nodeAliases, nodeId)),
          ),
          allocations: Object.freeze(
            route.stages.map((stage) =>
              Object.freeze({
                node: requiredAlias(nodeAliases, stage.nodeId),
                range: Object.freeze([stage.startLayer, stage.endLayerExclusive]),
              }),
            ),
          ),
          provenance: route.provenance,
        }),
      ),
    ),
    readiness: Object.freeze({
      route_ready: readiness.summary.routeReady,
      ready_for_runtime_load: readiness.summary.readyForRuntimeLoad,
      rows: Object.freeze(
        readiness.rows.map((row) =>
          Object.freeze({
            node: requiredAlias(nodeAliases, row.nodeId),
            assignment: row.assignment,
            states: Object.freeze(
              Object.fromEntries(
                Object.entries(row.cells).map(([stage, proof]) => [stage, proof.state]),
              ),
            ),
          }),
        ),
      ),
    }),
    incidents: Object.freeze(
      incidents.map((incident, index) =>
        Object.freeze({
          alias: `incident-${String(index + 1).padStart(3, '0')}`,
          mode: incident.mode,
          reported_status: incident.status,
          deployment: requiredAlias(deploymentAliases, incident.deploymentId),
          requests: Object.freeze(
            incident.requestIds.map((requestId) => requiredAlias(requestAliases, requestId)),
          ),
          old_route: requiredAlias(routeAliases, incident.oldRoute.id),
          replacement_route:
            incident.newRoute === null ? null : requiredAlias(routeAliases, incident.newRoute.id),
          detector_scope: incident.trigger.scope,
          claim_boundary: incident.claimBoundary,
        }),
      ),
    ),
    privacy: Object.freeze({
      identifiers_pseudonymized: true,
      coordinates_omitted: true,
      source_artifacts_omitted: true,
      local_paths_omitted: true,
    }),
  });
}

export type PseudonymizedExport = ReturnType<typeof createPseudonymizedExport>;
