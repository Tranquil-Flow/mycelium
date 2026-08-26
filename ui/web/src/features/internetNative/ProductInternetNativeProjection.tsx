import type { ProductRouteId } from '../../app/navigation';
import type { ProductSnapshot } from '../productEvidence/contracts';

const projectionNames: Readonly<Record<ProductRouteId, string>> = {
  lab: 'Internet-native bootstrap',
  network: 'Internet activation path (Network)',
  nodes: 'Internet member state (Nodes)',
  inference: 'Inference path',
  readiness: 'Internet-native readiness',
  plans: 'Internet-native plan path costs',
  incidents: 'Internet-native incidents',
  settings: 'Internet-native settings',
};

function metric(value: number | null, suffix = ''): string {
  return value === null ? 'unknown' : `${value}${suffix}`;
}

export function ProductInternetNativeProjection({
  view,
  snapshot,
}: {
  readonly view: ProductRouteId;
  readonly snapshot: ProductSnapshot;
}) {
  const evidence = snapshot.internet_native;
  const activation = evidence.activation_observation;
  const metrics = activation.metrics;
  const transitions = evidence.activation_history.some((observation, index, history) => (
    index > 0 && history[index - 1].path_class !== observation.path_class
  ));
  const reconnects = evidence.activation_history.some((observation, index, history) => (
    index > 0 && history[index - 1].connection_generation < observation.connection_generation
  ));

  return (
    <section className="panel" aria-label={projectionNames[view]}>
      <p className="eyebrow">Internet-native product evidence</p>
      <h2>{projectionNames[view]}</h2>
      {view === 'lab' ? (
        <dl>
          <div><dt>Bootstrap</dt><dd>{evidence.bootstrap_status.freshness}</dd></div>
          <div><dt>TLS</dt><dd>{evidence.bootstrap_status.tls_state}</dd></div>
          <div><dt>Invitation</dt><dd>{evidence.bootstrap_status.invitation_state}</dd></div>
          <div><dt>Requests</dt><dd>{evidence.bootstrap_status.counters.requests}</dd></div>
        </dl>
      ) : null}
      {view === 'network' ? (
        <dl>
          <div><dt>Path</dt><dd>{activation.path_class}</dd></div>
          <div><dt>Generation</dt><dd>{activation.connection_generation}</dd></div>
          <div aria-label="rtt_ms"><dt>RTT</dt><dd>{metric(metrics.rtt_ms, ' ms')}</dd></div>
          <div aria-label="goodput"><dt>Goodput</dt><dd>{metric(metrics.goodput_bytes_per_second, ' B/s')}</dd></div>
          <div aria-label="loss"><dt>Loss</dt><dd>{metric(metrics.loss_ratio)}</dd></div>
          <div><dt>Relay</dt><dd aria-label="relay reference">{evidence.relay_projection?.relay_reference ?? 'not observed'}</dd></div>
        </dl>
      ) : null}
      {view === 'nodes' ? (
        <p>Endpoint {activation.endpoint_pseudonym ?? 'unknown'} · {activation.freshness}</p>
      ) : null}
      {view === 'inference' ? (
        <p>{activation.path_class} · generation {activation.connection_generation} · reuse {activation.connection_reuse}</p>
      ) : null}
      {view === 'readiness' ? (
        <p>{evidence.qualification?.result ?? 'not qualified'} · {activation.freshness}</p>
      ) : null}
      {view === 'plans' ? (
        <p>{metric(metrics.rtt_ms, ' ms')} RTT · {metric(metrics.goodput_bytes_per_second, ' B/s')} goodput</p>
      ) : null}
      {view === 'incidents' ? (
        transitions || reconnects ? (
          <ul>
            {transitions ? <li>Activation path transition</li> : null}
            {reconnects ? <li>Bounded reconnect</li> : null}
          </ul>
        ) : <p>No internet-native path incidents.</p>
      ) : null}
      {view === 'settings' ? (
        <p>Projection privacy: pseudonymous endpoint and HMAC relay reference only.</p>
      ) : null}
    </section>
  );
}
