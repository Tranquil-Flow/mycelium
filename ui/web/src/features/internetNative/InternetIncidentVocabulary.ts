// SPDX-License-Identifier: AGPL-3.0-or-later
//
// InternetIncidentVocabulary — Incidents workspace bounded vocabulary.
//
// The eight-workspace incident projection uses only this closed set of
// internet-native incident codes. Outcomes are bounded text that never
// carries network identity material.

export const INTERNET_INCIDENT_CODES = [
  'bootstrap_unreachable',
  'pin_mismatch',
  'invite_rejected',
  'lease_stale',
  'path_transition',
  'reconnect',
  'revoked',
] as const;

export type InternetIncidentCode = (typeof INTERNET_INCIDENT_CODES)[number];

export interface InternetIncident {
  readonly code: InternetIncidentCode;
  readonly label: string;
  readonly outcome: string;
}

const INCIDENTS: Record<InternetIncidentCode, Omit<InternetIncident, 'code'>> = {
  bootstrap_unreachable: {
    label: 'Bootstrap unreachable',
    outcome: 'membership freshness follows the signed lease; no peer failure is fabricated',
  },
  pin_mismatch: {
    label: 'Seed key pin mismatch',
    outcome: 'join withheld; no invite secret was transmitted',
  },
  invite_rejected: {
    label: 'Invitation rejected',
    outcome: 'bounded reason code recorded; no partial member created',
  },
  lease_stale: {
    label: 'Membership lease stale',
    outcome: 'control blocked until renewal; measurements project unknown',
  },
  path_transition: {
    label: 'Activation path transition',
    outcome: 'prior observations retained; new generation recorded',
  },
  reconnect: {
    label: 'Bounded reconnect',
    outcome: 'same canonical origin retried; no alternate origin used',
  },
  revoked: {
    label: 'Membership revoked',
    outcome: 'control and activation admission removed for the generation',
  },
};

export function describeInternetIncident(code: string): InternetIncident {
  if (!(INTERNET_INCIDENT_CODES as readonly string[]).includes(code)) {
    throw new Error('internet_incident_code_invalid');
  }
  const incidentCode = code as InternetIncidentCode;
  return { code: incidentCode, ...INCIDENTS[incidentCode] };
}
