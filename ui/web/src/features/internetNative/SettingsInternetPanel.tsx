// SPDX-License-Identifier: AGPL-3.0-or-later
//
// SettingsInternetPanel — Settings workspace component.
//
// Operator-visible public-origin readiness, certificate freshness,
// bootstrap/relay policy labels, and owner-private invite/revocation entry
// points. Private diagnostics content is NEVER rendered (only the
// owner-private label). Raw credentials have no render path.

import type { FreshnessState } from './types';

export interface SettingsInternetPanelProps {
  readonly public_origin_ready: boolean | null;
  readonly certificate_freshness: FreshnessState;
  readonly bootstrap_policy: string | null;
  readonly relay_policy: string | null;
  readonly invite_entry: string;
  readonly revocation_entry: string;
  readonly show_private_diagnostics: boolean;
}

function readyLabel(value: boolean | null): string {
  if (value === null) return 'unknown';
  return value ? 'ready' : 'not ready';
}

function policyLabel(value: string | null): string {
  if (value === null) return 'unknown';
  return value;
}

export function SettingsInternetPanel(
  props: SettingsInternetPanelProps,
) {
  return (
    <section aria-label="Internet-native settings">
      <div aria-label="public origin readiness">{readyLabel(props.public_origin_ready)}</div>
      <div aria-label="certificate freshness">{props.certificate_freshness}</div>
      <div aria-label="bootstrap policy">{policyLabel(props.bootstrap_policy)}</div>
      <div aria-label="relay policy">{policyLabel(props.relay_policy)}</div>
      <div aria-label="invite entry">{props.invite_entry}</div>
      <div aria-label="revocation entry">{props.revocation_entry}</div>
      <div aria-label="private diagnostics">owner-private only</div>
    </section>
  );
}
