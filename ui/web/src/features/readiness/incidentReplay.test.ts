import { describe, expect, it } from 'vitest';
import { loadStaticObservatoryBundle } from '../../data/observatorySource';
import { projectIncidentReplay } from './incidentReplay';

describe('incident replay truthfulness', () => {
  const { incidents } = loadStaticObservatoryBundle();
  const active = incidents.find((incident) => incident.mode === 'active_failover')!;
  const circuit = incidents.find((incident) => incident.mode === 'circuit_break')!;

  it('keeps a reported replacement candidate non-active until cutover/resume evidence', () => {
    const candidateIndex = active.transitions.findIndex(
      (transition) => transition.state === 'CANDIDATE_SELECTED',
    );
    const candidate = projectIncidentReplay(active, candidateIndex);
    const final = projectIncidentReplay(active, active.transitions.length - 1);

    expect(candidate.replacement?.displayState).toBe('candidate');
    expect(candidate.replacement?.displayState).not.toBe('active');
    expect(final.replacement?.displayState).toBe('active');
  });

  it('never creates a replacement or reroute for a circuit break', () => {
    const replay = projectIncidentReplay(circuit, circuit.transitions.length - 1);

    expect(replay.replacement).toBeNull();
    expect(replay.outcome).toMatch(/503.*no reroute/i);
  });

  it('carries reported detector scope and status but no inferred severity', () => {
    const replay = projectIncidentReplay(active, 0);

    expect(replay.detectorScope).toBe(active.trigger.scope);
    expect(replay.reportedStatus).toBe(active.status);
    expect('severity' in replay).toBe(false);
  });
});
