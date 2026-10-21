// SPDX-License-Identifier: AGPL-3.0-only
import { describe, expect, it } from 'vitest';
import {
  APPLE_DEFERRED_BLOCKER,
  MobileProjectionError,
  RUNG_ORDER,
  WORKSPACES,
  projectAllWorkspaces,
  projectWorkspace,
  roleRow,
  rungRow,
  type MobileMember,
  type MobileRungSource,
} from './projections';

const ANDROID_REF = 'sha256:' + 'a'.repeat(64);

function android(overrides: Partial<MobileMember> = {}): MobileMember {
  return {
    memberRef: ANDROID_REF,
    platformFamily: 'android',
    lifecycle: 'foreground',
    rungs: {
      signed_membership: { decision: 'eligible', blockers: [], sourceKind: 'native_device_observation' },
      capability_eligibility: { decision: 'eligible', blockers: [], sourceKind: 'native_device_observation' },
      artifact_readiness: { decision: 'eligible', blockers: [], sourceKind: 'native_device_observation' },
      operation_qualification: { decision: 'eligible', blockers: [], sourceKind: 'native_device_observation' },
    },
    roles: {
      qualified_model_stage: { decision: 'eligible', blockers: [], sourceKind: 'native_device_observation' },
    },
    thermalMaxCelsius: 38.5,
    batteryPercent: null,
    relayOnline: true,
    incidents: [],
    ...overrides,
  };
}

function source(members: MobileMember[], freshness: MobileRungSource['freshness'] = 'live'): MobileRungSource {
  return { generation: 7, freshness, members };
}

describe('mobile rung projection', () => {
  it('reconstructs one generation in all eight workspaces', () => {
    const projections = projectAllWorkspaces(source([android()]));
    expect(projections.map((item) => item.workspace)).toEqual([...WORKSPACES]);
    expect(new Set(projections.map((item) => item.generation))).toEqual(new Set([7]));
  });

  it('renders each rung from its own record and never infers a later rung', () => {
    const member = android({
      rungs: {
        signed_membership: { decision: 'eligible', blockers: [], sourceKind: 'native_device_observation' },
      },
    });
    const rows = RUNG_ORDER.map((rung) => rungRow(member, rung));
    expect(rows[0].state).toBe('current');
    expect(rows.slice(1).map((row) => row.value)).toEqual(['unknown', 'unknown', 'unknown']);
    expect(rows.slice(1).every((row) => row.blockers.includes('rung_record_absent'))).toBe(true);
  });

  it('keeps synthetic or Termux evidence from showing an eligible operation', () => {
    const member = android({
      rungs: {
        ...android().rungs,
        operation_qualification: { decision: 'eligible', blockers: [], sourceKind: 'synthetic_conformance' },
      },
      roles: { qualified_model_stage: { decision: 'eligible', blockers: [], sourceKind: 'termux_conformance' } },
    });
    const operation = rungRow(member, 'operation_qualification');
    expect(operation.value).toBe('withheld');
    expect(operation.blockers).toContain('non_native_source_cannot_qualify');
    expect(roleRow(member, 'qualified_model_stage').state).toBe('blocked');
    const inference = projectWorkspace(source([member]), 'inference');
    expect(inference.rows[0].value).toBe('not participating');
  });

  it('shows Apple members as deferred and ineligible everywhere', () => {
    const apple = android({ memberRef: 'family:' + 'b'.repeat(32), platformFamily: 'ios' });
    const projections = projectAllWorkspaces(source([apple]));
    const deviceLab = projections.find((item) => item.workspace === 'device_lab');
    expect(deviceLab?.rows.every((row) => row.state === 'deferred')).toBe(true);
    expect(deviceLab?.rows[0].blockers).toEqual([APPLE_DEFERRED_BLOCKER]);
    expect(projections.find((item) => item.workspace === 'inference')?.rows[0].value).toBe('not participating');
    expect(projections.find((item) => item.workspace === 'settings')?.rows[0].value).toBe('disabled');
  });

  it('renders missing native metrics as unknown, never zero', () => {
    const nodes = projectWorkspace(source([android()]), 'nodes');
    const battery = nodes.rows.find((row) => row.label.endsWith('battery (%)'));
    expect(battery?.value).toBe('unknown');
    expect(battery?.blockers).toEqual(['native_observation_absent']);
  });

  it('rejects raw device identities instead of rendering them', () => {
    expect(() => projectAllWorkspaces(source([android({ memberRef: 'Pixel-8-Pro-of-alice' })]))).toThrow(
      MobileProjectionError,
    );
  });

  it('never names phone brands or internal gate labels', () => {
    const text = JSON.stringify(projectAllWorkspaces(source([android()])));
    expect(text).not.toMatch(/Pixel|Samsung|Google|\bA1[0-9]\b|Termux/);
  });

  it('carries incidents with member scope and stale freshness through', () => {
    const projection = projectWorkspace(
      source([android({ incidents: ['thermal_withdrawal', 'network_loss_reconnected'] })], 'stale'),
      'incidents',
    );
    expect(projection.freshness).toBe('stale');
    expect(projection.rows.map((row) => row.value)).toEqual(['thermal_withdrawal', 'network_loss_reconnected']);
  });
});
