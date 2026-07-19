import type { FailoverIncident } from '../../model/types';

export type ReplacementDisplayState = 'candidate' | 'loading' | 'activating' | 'active';

export interface IncidentReplayProjection {
  readonly incidentId: string;
  readonly cursor: number;
  readonly transitionState: string;
  readonly transitionDetail: string;
  readonly detectorScope: string;
  readonly reportedStatus: FailoverIncident['status'];
  readonly oldRoute: {
    readonly id: string;
    readonly generation: number;
    readonly nodeIds: readonly string[];
    readonly displayState: 'prior' | 'draining' | 'failed' | 'aborted';
  };
  readonly replacement: {
    readonly id: string;
    readonly generation: number;
    readonly nodeIds: readonly string[];
    readonly displayState: ReplacementDisplayState;
  } | null;
  readonly outcome: string;
}

function replacementState(state: string): ReplacementDisplayState | null {
  switch (state) {
    case 'CANDIDATE_SELECTED':
      return 'candidate';
    case 'REPLACEMENT_LOADING':
      return 'loading';
    case 'CUTOVER_STARTED':
      return 'activating';
    case 'RESUMED':
      return 'active';
    default:
      return null;
  }
}

export function projectIncidentReplay(
  incident: FailoverIncident,
  requestedCursor: number,
): IncidentReplayProjection {
  if (incident.transitions.length === 0) throw new TypeError('Incident replay requires transitions');
  const cursor = Math.max(0, Math.min(requestedCursor, incident.transitions.length - 1));
  const transition = incident.transitions[cursor];
  let displayState: ReplacementDisplayState | null = null;
  for (let index = 0; index <= cursor; index += 1) {
    displayState = replacementState(incident.transitions[index].state) ?? displayState;
  }
  const replacement =
    incident.newRoute === null || displayState === null
      ? null
      : Object.freeze({
          id: incident.newRoute.id,
          generation: incident.newRoute.generation,
          nodeIds: Object.freeze([...incident.newRoute.nodeIds]),
          displayState,
        });
  const oldDisplayState =
    incident.mode === 'stable_drain'
      ? 'draining'
      : incident.mode === 'active_failover'
        ? 'failed'
        : incident.mode === 'circuit_break'
          ? 'aborted'
          : 'prior';
  const outcome =
    incident.mode === 'circuit_break'
      ? 'Output rejected · 503 · no reroute claimed'
      : transition.state === 'RESUMED'
        ? `Request resumed on reported route generation ${incident.newRoute?.generation}.`
        : `No resume proven at ${transition.state.replaceAll('_', ' ')}.`;

  return Object.freeze({
    incidentId: incident.id,
    cursor,
    transitionState: transition.state,
    transitionDetail: transition.detail,
    detectorScope: incident.trigger.scope,
    reportedStatus: incident.status,
    oldRoute: Object.freeze({
      id: incident.oldRoute.id,
      generation: incident.oldRoute.generation,
      nodeIds: Object.freeze([...incident.oldRoute.nodeIds]),
      displayState: oldDisplayState,
    }),
    replacement,
    outcome,
  });
}
