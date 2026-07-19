import { describe, expect, it } from 'vitest';
import { loadStaticObservatoryBundle } from '../../data/observatorySource';
import {
  READINESS_STAGES,
  buildReadinessModel,
  type ReadinessState,
} from './readinessModel';

describe('strict readiness model', () => {
  const { provisioning } = loadStaticObservatoryBundle();

  it('keeps the mandated ladder order and independent proof states', () => {
    const model = buildReadinessModel(provisioning);

    expect(READINESS_STAGES.map((stage) => stage.label)).toEqual([
      'Discovered',
      'Planned',
      'Assigned',
      'Artifacts verified',
      'Runtime loaded',
      'Stage probed',
      'Route challenged',
      'Route ready',
    ]);
    expect(model.rows).toHaveLength(provisioning.nodeIds.length);
    expect(model.rows[0].cells.artifacts_verified.state).toBe('PROVEN');
    expect(model.rows[0].cells.runtime_loaded.state).toBe('NOT_PROVEN');
    expect(model.rows[0].cells.route_ready.state).toBe('NOT_PROVEN');
  });

  it('never promotes ready-for-load or artifact verification to a runtime proof', () => {
    const model = buildReadinessModel({
      ...provisioning,
      allAssignmentsVerified: true,
      readyForRuntimeLoad: true,
      routeReady: false,
    });

    for (const row of model.rows) {
      expect(row.cells.artifacts_verified.state).toBe('PROVEN');
      expect(row.cells.runtime_loaded.state).toBe('NOT_PROVEN');
      expect(row.cells.stage_probed.state).toBe('NOT_PROVEN');
      expect(row.cells.route_challenged.state).toBe('NOT_PROVEN');
      expect(row.cells.route_ready.state).toBe('NOT_PROVEN');
    }
    expect(model.summary.routeReady).toBe(false);
    expect(model.summary.missingProofs).toEqual([
      'Runtime loaded',
      'Stage probed',
      'Route challenged',
      'Route ready',
    ]);
  });

  it('uses the complete five-state vocabulary without treating false as failed', () => {
    const states: ReadinessState[] = [
      'PROVEN',
      'NOT_PROVEN',
      'FAILED',
      'NOT_APPLICABLE',
      'CONFLICT',
    ];
    expect(states).toHaveLength(5);
    expect(buildReadinessModel(provisioning).rows[0].cells.route_ready.state).toBe('NOT_PROVEN');
  });
});
