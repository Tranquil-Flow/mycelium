// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from 'vitest';
import {
  INTERNET_INCIDENT_CODES,
  describeInternetIncident,
} from './InternetIncidentVocabulary';

describe('InternetIncidentVocabulary (Incidents workspace)', () => {
  it('exposes exactly the bounded incident vocabulary', () => {
    expect(INTERNET_INCIDENT_CODES).toEqual([
      'bootstrap_unreachable',
      'pin_mismatch',
      'invite_rejected',
      'lease_stale',
      'path_transition',
      'reconnect',
      'revoked',
    ]);
  });

  it('describes every code with bounded outcome text', () => {
    for (const code of INTERNET_INCIDENT_CODES) {
      const incident = describeInternetIncident(code);
      expect(incident.code).toBe(code);
      expect(incident.label.length).toBeGreaterThan(0);
      expect(incident.outcome.length).toBeGreaterThan(0);
      expect(incident.label.length).toBeLessThanOrEqual(128);
      expect(incident.outcome.length).toBeLessThanOrEqual(256);
    }
  });

  it('rejects codes outside the vocabulary', () => {
    expect(() => describeInternetIncident('not_an_incident')).toThrow();
    expect(() => describeInternetIncident('tailnet_fallback')).toThrow();
  });

  it('outcome text carries no raw identity material', () => {
    for (const code of INTERNET_INCIDENT_CODES) {
      const incident = describeInternetIncident(code);
      expect(incident.label).not.toMatch(/https?:\/\//);
      expect(incident.outcome).not.toMatch(/\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/);
      expect(incident.outcome).not.toContain('token');
    }
  });
});
