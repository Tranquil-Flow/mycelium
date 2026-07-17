import { describe, expect, it } from 'vitest';
import audit from '../../../tests/fixtures/source/provisioning-audit.json';
import routePlan from '../../../tests/fixtures/source/route-plan-v2.json';
import { adaptProvisioningEvidence } from './provisioning';

describe('provisioning evidence', () => {
  it('joins route plan and audit under their own explicit deployment scope', () => {
    const evidence = adaptProvisioningEvidence(routePlan, audit);

    expect(evidence.protocol).toBe('mycelium.ui_provisioning_evidence.v1');
    expect(evidence.model.id).toBe('bumblebee-testing/tiny-random-GPT2Model-sharded');
    expect(evidence.nodeIds).toEqual(['http-peer-a', 'http-peer-b']);
    expect(evidence.readyForRuntimeLoad).toBe(true);
    expect(evidence.routeReady).toBe(false);
    expect(evidence.provenance).toBe('declared');
    expect(evidence.claimBoundary).toContain('artifact provisioning only');
    expect(Object.isFrozen(evidence)).toBe(true);
    expect(Object.isFrozen(evidence.nodeIds)).toBe(true);
  });

  it('rejects an audit that does not match its route plan or contradicts readiness', () => {
    const wrongNodes = structuredClone(audit);
    wrongNodes.verified_nodes = ['http-peer-a'];
    expect(() => adaptProvisioningEvidence(routePlan, wrongNodes)).toThrow(/verified_nodes/);

    const contradictory = { ...structuredClone(audit), errors: ['digest mismatch'] };
    expect(() => adaptProvisioningEvidence(routePlan, contradictory)).toThrow(/errors/);

    const invalidTimestamp = structuredClone(audit);
    invalidTimestamp.timestamp = 'not-a-time';
    expect(() => adaptProvisioningEvidence(routePlan, invalidTimestamp)).toThrow(/timestamp/);
  });

  it('rejects incomplete or overlapping route layer coverage', () => {
    const brokenRoute = structuredClone(routePlan);
    brokenRoute.route[1].range.start_layer = 2;
    expect(() => adaptProvisioningEvidence(brokenRoute, audit)).toThrow(/route\[1\]\.range/);
  });
});
