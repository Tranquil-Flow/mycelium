import { describe, expect, it } from 'vitest';
import { loadStaticObservatoryBundle } from '../../data/observatorySource';
import { createPseudonymizedExport } from './pseudonymizedExport';

describe('pseudonymized evidence export', () => {
  const bundle = loadStaticObservatoryBundle();

  it('replaces node, request, route, deployment, and model identifiers deterministically', () => {
    const exported = createPseudonymizedExport(
      bundle.snapshot,
      bundle.provisioning,
      bundle.incidents,
    );
    const serialized = JSON.stringify(exported);

    for (const node of bundle.snapshot.nodes) expect(serialized).not.toContain(node.id);
    for (const nodeId of bundle.provisioning.nodeIds) expect(serialized).not.toContain(nodeId);
    for (const incident of bundle.incidents) {
      expect(serialized).not.toContain(incident.deploymentId);
      for (const requestId of incident.requestIds) expect(serialized).not.toContain(requestId);
    }
    expect(exported.nodes[0].alias).toMatch(/^node-\d{3}$/);
    expect(exported.protocol).toBe('mycelium.ui_pseudonymized_export.v1');
  });

  it('omits coordinates, raw locators, raw source, prompts, tokens, and local paths', () => {
    const exported = createPseudonymizedExport(
      bundle.snapshot,
      bundle.provisioning,
      bundle.incidents,
    );
    const serialized = JSON.stringify(exported).toLowerCase();

    expect(serialized).not.toContain('latitude');
    expect(serialized).not.toContain('longitude');
    expect(serialized).not.toContain('/users/');
    expect(serialized).not.toContain('prompt');
    expect(serialized).not.toContain('raw_source');
    expect(serialized).toContain('claim_boundary');
  });
});
