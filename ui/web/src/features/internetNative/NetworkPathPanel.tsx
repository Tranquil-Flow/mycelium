// SPDX-License-Identifier: AGPL-3.0-or-later
//
// NetworkPathPanel — Network workspace component.
//
// Renders the directed path class, nullable measurements (unknown-not-zero),
// and the privacy-safe relay reference. Raw relay URLs, IPs, ports, or DNS
// names can never render: the relay reference is accepted only as
// `hmac-sha256:` + 64 hex characters.

import type { InternetActivationObservation, RelayProjection } from './types';
import { renderActivationObservation, validateRelayReference } from './projections';

export interface NetworkPathPanelProps {
  readonly observation: InternetActivationObservation | null;
  readonly relay: RelayProjection | null;
}

export function NetworkPathPanel(props: NetworkPathPanelProps) {
  const observation = props.observation;
  const relayReference =
    props.relay !== null && validateRelayReference(props.relay.relay_reference)
      ? props.relay.relay_reference
      : null;
  if (observation === null) {
    return (
      <section aria-label="Internet activation path (Network)">
        <div aria-label="path class">unknown</div>
        <div aria-label="rtt_ms">unknown</div>
        <div aria-label="warm_rtt_ms">unknown</div>
        <div aria-label="jitter_ms">unknown</div>
        <div aria-label="goodput">unknown</div>
        <div aria-label="loss">unknown</div>
        <div aria-label="relay reference">unknown</div>
      </section>
    );
  }
  const rendered = renderActivationObservation(observation, props.relay);
  return (
    <section aria-label="Internet activation path (Network)">
      <div aria-label="path class">{rendered.path_class}</div>
      <div aria-label="rtt_ms">{rendered.rtt_ms}</div>
      <div aria-label="warm_rtt_ms">{rendered.warm_rtt_ms}</div>
      <div aria-label="jitter_ms">{rendered.jitter_ms}</div>
      <div aria-label="goodput">{rendered.goodput_bytes_per_second}</div>
      <div aria-label="loss">{rendered.loss_ratio}</div>
      <div aria-label="connection generation">{observation.connection_generation}</div>
      <div aria-label="connection reuse">{observation.connection_reuse}</div>
      <div aria-label="relay reference">
        {relayReference === null ? 'unknown' : relayReference}
      </div>
    </section>
  );
}
