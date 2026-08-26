// SPDX-License-Identifier: AGPL-3.0-or-later
//
// NodesInternetPanel — Nodes workspace component.
//
// Renders the member's current incarnation/generation, the EndpointID
// PSEUDONYM (never the raw id), lease freshness, and qualification state.
// No address or hostname field exists in this projection.

import type { FreshnessState } from './types';
import { renderPseudonym } from './projections';

export interface NodesInternetPanelProps {
  readonly incarnation: string | null;
  readonly generation: number | null;
  readonly endpoint_pseudonym: string | null;
  readonly lease_freshness: FreshnessState;
  readonly qualification: string | null;
}

function renderText(value: string | number | null): string {
  if (value === null) return 'unknown';
  return String(value);
}

export function NodesInternetPanel(props: NodesInternetPanelProps) {
  return (
    <section aria-label="Internet member state (Nodes)">
      <div aria-label="incarnation">{renderText(props.incarnation)}</div>
      <div aria-label="generation">{renderText(props.generation)}</div>
      <div aria-label="endpoint">{renderPseudonym(props.endpoint_pseudonym)}</div>
      <div aria-label="lease freshness">{props.lease_freshness}</div>
      <div aria-label="qualification">{renderText(props.qualification)}</div>
    </section>
  );
}
