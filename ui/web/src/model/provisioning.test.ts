import { describe, expect, it } from 'vitest';
import canonicalManualProvisioningRoute from '../../../../contracts/compatibility-fixtures/manual-provisioning-route-v1.json';
import canonicalProvisioningAudit from '../../../../contracts/compatibility-fixtures/provisioning-audit-v1.json';
import historicalAudit from '../../../tests/fixtures/source/provisioning-audit.json';
import { adaptProvisioningEvidence } from './provisioning';

describe('provisioning evidence', () => {
  it('consumes the canonical generated route and audit directly', () => {
    const evidence = adaptProvisioningEvidence(
      canonicalManualProvisioningRoute,
      canonicalProvisioningAudit,
    );

    expect(evidence.protocol).toBe('mycelium.ui_provisioning_evidence.v1');
    expect(evidence.model).toEqual({
      id: 'org/model',
      numLayers: 4,
      manifestDigest: 'sha256:ef766c09c58656512fe01f1d12199e03de5e779ca785eee16ac0b63129872356',
      resolvedCommit: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    });
    expect(evidence.nodeIds).toEqual(['node-a', 'node-b']);
    expect(evidence.assignments).toEqual([
      { nodeId: 'node-a', startLayer: 0, endLayerExclusive: 2, layerCount: 2 },
      { nodeId: 'node-b', startLayer: 2, endLayerExclusive: 4, layerCount: 2 },
    ]);
    expect(evidence.readyForRuntimeLoad).toBe(true);
    expect(evidence.routeReady).toBe(false);
    expect(evidence.provenance).toBe('declared');
    expect(evidence.claimBoundary).toContain('artifact provisioning only');
    expect(Object.isFrozen(evidence)).toBe(true);
    expect(Object.isFrozen(evidence.nodeIds)).toBe(true);
  });

  it('derives route order from stages when the optional node_order is omitted', () => {
    const routeWithoutNodeOrder = structuredClone(canonicalManualProvisioningRoute) as Partial<
      typeof canonicalManualProvisioningRoute
    >;
    delete routeWithoutNodeOrder.node_order;

    const evidence = adaptProvisioningEvidence(routeWithoutNodeOrder, canonicalProvisioningAudit);

    expect(evidence.nodeIds).toEqual(['node-a', 'node-b']);
  });

  it('keeps the historical UI audit fixture bound to deterministic deployment assignments', () => {
    expect(historicalAudit.model).toEqual({
      model_id: 'bumblebee-testing/tiny-random-GPT2Model-sharded',
      num_layers: 5,
      manifest_digest: 'sha256:75a58126d140ab3b945d80482314a92d6047fa163d37c2b37d0bfc4c420ddf46',
      resolved_commit: '4fca22a84867aacca5dcf7317144782ea1807e1a',
    });
    expect(historicalAudit.deployment_id).toBe('382b6079-1ebb-50b2-9a8e-99ff36cea86a');
    expect(historicalAudit.deployment_epoch).toBe(1);
    expect(historicalAudit.assignment_bindings).toEqual([
      {
        assignment_id: '202e4eb3-2e17-5973-9d7a-18483d73b8ca',
        node_id: 'http-peer-a',
        range: { start_layer: 0, end_layer_exclusive: 3, layer_count: 3 },
      },
      {
        assignment_id: 'ae0333c5-2de7-5d43-a176-3f3bb0537f16',
        node_id: 'http-peer-b',
        range: { start_layer: 3, end_layer_exclusive: 5, layer_count: 2 },
      },
    ]);
  });

  it('rejects malformed immutable route model identity', () => {
    const wrongDigest = structuredClone(canonicalManualProvisioningRoute);
    wrongDigest.model.manifest_digest = 'not-a-digest';
    expect(() => adaptProvisioningEvidence(wrongDigest, canonicalProvisioningAudit)).toThrow(
      /manifest_digest/,
    );

    const wrongCommit = structuredClone(canonicalManualProvisioningRoute);
    wrongCommit.model.resolved_commit = 'main';
    expect(() => adaptProvisioningEvidence(wrongCommit, canonicalProvisioningAudit)).toThrow(
      /resolved_commit/,
    );
  });

  it('rejects every audit model identity field that differs from the route', () => {
    const wrongModelId = structuredClone(canonicalProvisioningAudit);
    wrongModelId.model.model_id = 'org/other-model';
    expect(() => adaptProvisioningEvidence(canonicalManualProvisioningRoute, wrongModelId)).toThrow(
      /audit\.model\.model_id/,
    );

    const wrongLayerCount = structuredClone(canonicalProvisioningAudit);
    wrongLayerCount.model.num_layers = 5;
    expect(() =>
      adaptProvisioningEvidence(canonicalManualProvisioningRoute, wrongLayerCount),
    ).toThrow(/audit\.model\.num_layers/);

    const wrongDigest = structuredClone(canonicalProvisioningAudit);
    wrongDigest.model.manifest_digest = `sha256:${'f'.repeat(64)}`;
    expect(() => adaptProvisioningEvidence(canonicalManualProvisioningRoute, wrongDigest)).toThrow(
      /audit\.model\.manifest_digest/,
    );

    const wrongCommit = structuredClone(canonicalProvisioningAudit);
    wrongCommit.model.resolved_commit = 'b'.repeat(40);
    expect(() => adaptProvisioningEvidence(canonicalManualProvisioningRoute, wrongCommit)).toThrow(
      /audit\.model\.resolved_commit/,
    );
  });

  it('rejects missing or invalid deployment and assignment identity', () => {
    const missingDeploymentId = structuredClone(canonicalProvisioningAudit) as {
      deployment_id?: string;
    } & Omit<typeof canonicalProvisioningAudit, 'deployment_id'>;
    delete missingDeploymentId.deployment_id;
    expect(() =>
      adaptProvisioningEvidence(canonicalManualProvisioningRoute, missingDeploymentId),
    ).toThrow(/audit\.deployment_id/);

    const invalidDeploymentId = structuredClone(canonicalProvisioningAudit);
    invalidDeploymentId.deployment_id = 'not-a-uuid';
    expect(() =>
      adaptProvisioningEvidence(canonicalManualProvisioningRoute, invalidDeploymentId),
    ).toThrow(/audit\.deployment_id/);

    const invalidDeploymentEpoch = structuredClone(canonicalProvisioningAudit);
    invalidDeploymentEpoch.deployment_epoch = -1;
    expect(() =>
      adaptProvisioningEvidence(canonicalManualProvisioningRoute, invalidDeploymentEpoch),
    ).toThrow(/audit\.deployment_epoch/);

    const invalidAssignmentId = structuredClone(canonicalProvisioningAudit);
    invalidAssignmentId.assignment_bindings[0].assignment_id = 'not-a-uuid';
    expect(() =>
      adaptProvisioningEvidence(canonicalManualProvisioningRoute, invalidAssignmentId),
    ).toThrow(/audit\.assignment_bindings\[0\]\.assignment_id/);
  });

  it('rejects audit binding order or ranges that differ from the route', () => {
    const wrongOrder = structuredClone(canonicalProvisioningAudit);
    wrongOrder.assignment_bindings.reverse();
    expect(() => adaptProvisioningEvidence(canonicalManualProvisioningRoute, wrongOrder)).toThrow(
      /audit\.assignment_bindings\[0\]\.node_id/,
    );

    const wrongRange = structuredClone(canonicalProvisioningAudit);
    wrongRange.assignment_bindings[0].range.end_layer_exclusive = 3;
    wrongRange.assignment_bindings[0].range.layer_count = 3;
    expect(() => adaptProvisioningEvidence(canonicalManualProvisioningRoute, wrongRange)).toThrow(
      /audit\.assignment_bindings\[0\]\.range/,
    );
  });

  it('rejects verified-node or binding-node mismatch', () => {
    const wrongVerifiedNodes = structuredClone(canonicalProvisioningAudit);
    wrongVerifiedNodes.verified_nodes = ['node-a'];
    expect(() =>
      adaptProvisioningEvidence(canonicalManualProvisioningRoute, wrongVerifiedNodes),
    ).toThrow(/audit\.verified_nodes/);

    const wrongBindingNode = structuredClone(canonicalProvisioningAudit);
    wrongBindingNode.assignment_bindings[0].node_id = 'node-c';
    expect(() =>
      adaptProvisioningEvidence(canonicalManualProvisioningRoute, wrongBindingNode),
    ).toThrow(/audit\.assignment_bindings\[0\]\.node_id/);
  });

  it('rejects contradictory readiness, errors, and invalid timestamps', () => {
    const readyWithoutVerification = structuredClone(canonicalProvisioningAudit);
    readyWithoutVerification.all_assignments_verified = false;
    expect(() =>
      adaptProvisioningEvidence(canonicalManualProvisioningRoute, readyWithoutVerification),
    ).toThrow(/ready_for_runtime_load/);

    const routeReadyWithoutLoad = structuredClone(canonicalProvisioningAudit);
    routeReadyWithoutLoad.ready_for_runtime_load = false;
    routeReadyWithoutLoad.route_ready = true;
    expect(() =>
      adaptProvisioningEvidence(canonicalManualProvisioningRoute, routeReadyWithoutLoad),
    ).toThrow(/route_ready/);

    const contradictoryErrors = {
      ...structuredClone(canonicalProvisioningAudit),
      errors: ['digest mismatch'],
    };
    expect(() =>
      adaptProvisioningEvidence(canonicalManualProvisioningRoute, contradictoryErrors),
    ).toThrow(/errors/);

    const invalidTimestamp = structuredClone(canonicalProvisioningAudit);
    invalidTimestamp.timestamp = 'not-a-time';
    expect(() =>
      adaptProvisioningEvidence(canonicalManualProvisioningRoute, invalidTimestamp),
    ).toThrow(/timestamp/);
  });

  it('rejects incomplete or overlapping route layer coverage', () => {
    const brokenRoute = structuredClone(canonicalManualProvisioningRoute);
    brokenRoute.route[1].range.start_layer = 1;
    expect(() => adaptProvisioningEvidence(brokenRoute, canonicalProvisioningAudit)).toThrow(
      /route\[1\]\.range/,
    );
  });
});