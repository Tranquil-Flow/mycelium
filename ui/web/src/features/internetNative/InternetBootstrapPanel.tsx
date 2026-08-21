// SPDX-License-Identifier: AGPL-3.0-or-later
//
// InternetBootstrapPanel — Device Lab workspace component.
//
// Renders the four A8 §9 device-lab projections:
//   * HTTPS bootstrap reachability (route_state)
//   * seed-pin verification
//   * invite state
//   * membership vs activation eligibility separation
//   * external-device preflight (canonical_origin_verified && tls_state publicly_trusted)
//
// Every nullable input renders as "unknown"; counters are rendered via
// `renderMetric` so a null never becomes "0". No raw URL, hostname, or
// credential is ever present in the rendered DOM.

import type { InternetBootstrapStatus } from './types';
import { renderBootstrapStatus, renderMetric } from './projections';

export interface InternetBootstrapPanelProps {
  readonly bootstrapStatus: InternetBootstrapStatus | null;
  readonly isMember: boolean | null;
  readonly activationEligible: boolean | null;
}

type PreflightState = 'ready' | 'blocked' | 'unknown';

function preflightState(status: InternetBootstrapStatus | null): PreflightState {
  if (status === null) return 'unknown';
  if (!status.canonical_origin_verified) return 'blocked';
  if (status.tls_state !== 'publicly_trusted') return 'blocked';
  return 'ready';
}

function yesNoUnknown(value: boolean | null): string {
  if (value === null) return 'unknown';
  return value ? 'yes' : 'no';
}

export function InternetBootstrapPanel({
  bootstrapStatus,
  isMember,
  activationEligible,
}: InternetBootstrapPanelProps) {
  const rendered = renderBootstrapStatus(bootstrapStatus);
  const preflight = preflightState(bootstrapStatus);

  return (
    <section aria-labelledby="internet-bootstrap-title" data-internet-bootstrap>
      <h3 id="internet-bootstrap-title">Internet-native bootstrap</h3>
      <dl>
        <div>
          <dt>Bootstrap reachability</dt>
          <dd data-field="route_state">{rendered.route_state}</dd>
        </div>
        <div>
          <dt>TLS state</dt>
          <dd data-field="tls_state">{rendered.tls_state}</dd>
        </div>
        <div>
          <dt>Canonical origin</dt>
          <dd data-field="canonical_origin_verified">{rendered.canonical_origin_verified}</dd>
        </div>
        <div>
          <dt>Seed pin state</dt>
          <dd data-field="seed_pin_state">{rendered.seed_pin_state}</dd>
        </div>
        <div>
          <dt>Invite state</dt>
          <dd data-field="invitation_state">{rendered.invitation_state}</dd>
        </div>
        <div>
          <dt>External-device preflight</dt>
          <dd data-field="preflight">{preflight}</dd>
        </div>
        <div>
          <dt>Member</dt>
          <dd data-field="is_member">{yesNoUnknown(isMember)}</dd>
        </div>
        <div>
          <dt>Activation eligible</dt>
          <dd data-field="activation_eligible">{yesNoUnknown(activationEligible)}</dd>
        </div>
      </dl>
      {bootstrapStatus !== null ? (
        <p data-eligibility-note>
          {activationEligible
            ? 'Activation admission is granted; capability authority remains the qualifier.'
            : isMember
              ? 'Membership is visible but activation remains ineligible without a current activation observation.'
              : 'A successful invite is required before any membership or activation claim.'}
        </p>
      ) : null}
      <div aria-label="Bootstrap counters">
        <span>requests {renderMetric(null)}</span>
        <span>joins accepted {rendered.counters.joins_accepted}</span>
        <span>joins rejected {rendered.counters.joins_rejected}</span>
      </div>
    </section>
  );
}

export default InternetBootstrapPanel;