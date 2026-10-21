// SPDX-License-Identifier: AGPL-3.0-only
import type { ProductQualification } from '../../app/contracts';
import type { ProductSnapshot } from '../productEvidence/contracts';
import { RouteExplorerWorkspace } from '../routeExplorer/RouteExplorer';
import type {
  ConsumerRouteStatusFacts,
  ConsumerRouteStatusState,
} from './consumerObservatory';
import {
  projectConsumerRouteFacts,
  type ConsumerDeviceFacts,
  type ConsumerRouteFacts,
} from './consumerRouteFacts';
import styles from './ConsumerDemoPage.module.css';

/** Reads the real route-status document; failures surface as named codes. */
export type ConsumerRouteStatusPort = (
  signal?: AbortSignal,
) => Promise<ConsumerRouteStatusFacts>;

const text = (value: string | number | boolean | null): string =>
  value === null ? 'Unavailable' : String(value);

const layers = (start: number | null, end: number | null): string =>
  start === null || end === null ? 'Unavailable' : `${start} – ${end} (end exclusive)`;

function deviceLabel(device: ConsumerDeviceFacts | null): string {
  if (device === null) return 'Unknown';
  const parts = [device.label ?? device.entity_id, device.peer_class, device.transport]
    .filter((part): part is string => part !== null);
  return parts.join(' · ');
}

function Fact({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd aria-label={label}>{value}</dd>
    </div>
  );
}

/**
 * The consumer live-route panel: route/generation/qualification identity from
 * `/api/v1/qualification/current` and the observatory snapshot, stage and node
 * placement from the product snapshot's `stage`/`device` entities and their
 * `placed_on` relations, plus the composed read-only `routeExplorer` feature.
 *
 * Missing evidence stays missing: an absent layer split, absent host name or
 * absent placement renders as unknown, never as a guessed value.
 */
export function ConsumerRoutePanel({
  qualification,
  snapshot,
  status,
  onCheck,
}: {
  readonly qualification: ProductQualification | null;
  readonly snapshot: ProductSnapshot | null;
  readonly status: ConsumerRouteStatusState;
  readonly onCheck: () => void;
}) {
  const facts: ConsumerRouteFacts = projectConsumerRouteFacts({
    qualification,
    snapshot,
    route_status: status.phase === 'ready' ? status.facts : null,
  });

  return (
    <section className={styles.routeFacts} aria-labelledby="consumer-route-facts-title">
      <p className={styles.eyebrow}>02 / Behind the answer</p>
      <h2 id="consumer-route-facts-title">Route, not guesswork.</h2>
      <p className={styles.feed} role="status">
        {status.phase === 'loading' ? 'Reading live route status'
          : status.phase === 'ready' ? 'Live route status read'
          : `Route status unavailable · ${status.code}`}
      </p>
      <button type="button" onClick={onCheck}>Check route status</button>

      <h3>Route and qualification identity</h3>
      <dl className={styles.facts}>
        <Fact label="Model" value={text(facts.model_id)} />
        <Fact label="Resolved commit" value={text(facts.resolved_commit)} />
        <Fact label="Route identity" value={text(facts.route_id)} />
        <Fact label="Route generation" value={text(facts.route_generation)} />
        <Fact label="Topology version" value={text(facts.topology_version)} />
        <Fact label="Deployment" value={text(facts.deployment_id)} />
        <Fact label="Deployment epoch" value={text(facts.deployment_epoch)} />
        <Fact label="Qualification binding" value={text(facts.qualification_id)} />
        <Fact label="Qualification digest" value={text(facts.qualification_digest)} />
        <Fact label="Evidence class" value={text(facts.evidence_class)} />
        <Fact label="Route ready" value={text(facts.route_ready)} />
        <Fact label="Manifest digest" value={text(facts.manifest_digest)} />
        <Fact label="Path manifest digest" value={text(facts.path_manifest_digest)} />
        <Fact label="Stage load proofs" value={text(facts.stage_load_proof_count)} />
        <Fact label="Decode mode" value={text(facts.decode_mode)} />
        <Fact label="Placement provenance" value={text(facts.placement_provenance)} />
        <Fact label="Route alive" value={text(facts.route_alive)} />
        <Fact label="Product snapshot generation" value={text(facts.snapshot_generation)} />
        <Fact
          label="Route status generation"
          value={status.phase === 'ready' ? String(status.facts.generation) : 'Unavailable'}
        />
      </dl>
      {facts.reason_codes.length > 0
        ? <p className={styles.hint}>Qualification reason codes: {facts.reason_codes.join(', ')}.</p>
        : null}

      <h3>Which node ran which layers</h3>
      {facts.layer_split === 'unknown' ? (
        <p className={styles.hint}>
          The current route, request-event and route-explorer documents publish no stage or layer
          split for this route. No layer-to-node split is inferred from peer counts, digests or
          transport endpoints.
        </p>
      ) : (
        <table className={styles.routeTable} aria-label="Stage layer placement">
          <caption>Stages as published by the product snapshot. Placement comes only from the stage's own placed_on relation.</caption>
          <thead>
            <tr>
              <th scope="col">Stage</th>
              <th scope="col">Layers</th>
              <th scope="col">Component roles</th>
              <th scope="col">Decode mode</th>
              <th scope="col">Runs on</th>
            </tr>
          </thead>
          <tbody>
            {facts.stages.map((stage) => (
              <tr key={stage.stage_entity_id} data-consumer-stage={stage.stage_entity_id}>
                <th scope="row">{text(stage.stage_index)}</th>
                <td>{layers(stage.start_layer, stage.end_layer_exclusive)}</td>
                <td>{stage.component_roles === null || stage.component_roles.length === 0 ? 'Unavailable' : stage.component_roles.join(', ')}</td>
                <td>{text(stage.decode_mode)}</td>
                <td>{deviceLabel(stage.device)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {facts.layer_split === 'available' && facts.stage_placement === 'unknown' ? (
        <p className={styles.hint}>
          Stages are published, but no stage carries a placement relation. Host identity for every
          stage is unknown.
        </p>
      ) : null}

      <h3>Nodes seen in the published route</h3>
      {facts.devices.length === 0 ? (
        <p className={styles.hint}>
          No device entity is published for this route. Mac names, hostnames and OS identities are
          not part of the published contract.
        </p>
      ) : (
        <table className={styles.routeTable} aria-label="Published route nodes">
          <caption>Public device identities only. This is not a count of Macs, and a device is not proof of involvement in this request.</caption>
          <thead>
            <tr>
              <th scope="col">Node</th>
              <th scope="col">Peer class</th>
              <th scope="col">Transport</th>
              <th scope="col">Runtime</th>
              <th scope="col">Lifecycle</th>
              <th scope="col">Lease</th>
              <th scope="col">Placement</th>
            </tr>
          </thead>
          <tbody>
            {facts.devices.map((device) => (
              <tr key={device.entity_id} data-consumer-node={device.entity_id}>
                <th scope="row">{device.label ?? device.entity_id}</th>
                <td>{text(device.peer_class)}</td>
                <td>{text(device.transport)}</td>
                <td>{text(device.runtime_backend)}</td>
                <td>{text(device.lifecycle)}</td>
                <td>{text(device.lease_freshness)}</td>
                <td>{text(device.placement_id)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Route status as the origin reports it</h3>
      {status.phase !== 'ready' ? (
        <p className={styles.hint}>
          {status.phase === 'loading'
            ? 'No route-status document has been read yet.'
            : `No route-status document was read (${status.code}); ${status.detail}. Nothing is substituted for it.`}
        </p>
      ) : (
        <dl className={styles.facts}>
          <Fact label="Route ready (status document)" value={text(status.facts.route_ready)} />
          <Fact label="Request sessions" value={String(status.facts.sessions.total)} />
          <Fact label="Active sessions" value={String(status.facts.sessions.by_state.accepted + status.facts.sessions.by_state.streaming)} />
          <Fact label="Failed sessions" value={String(status.facts.sessions.by_state.failed)} />
          <Fact label="Cancelled sessions" value={String(status.facts.sessions.by_state.cancelled)} />
          <Fact label="Quarantined sessions" value={String(status.facts.sessions.by_state.quarantined)} />
          <Fact label="Route status incidents" value={String(status.facts.incidents.length)} />
          <Fact label="Observed at (Unix ms)" value={String(status.facts.observed_at_unix_ms)} />
        </dl>
      )}
      {status.phase === 'ready' && status.facts.incidents.length > 0 ? (
        <ul className={styles.incidentList}>
          {status.facts.incidents.map((incident) => (
            <li key={`${incident.source_cursor}-${incident.reason}`}>
              <code>{incident.reason}</code> · {incident.protocol} · cursor {incident.source_cursor}
            </li>
          ))}
        </ul>
      ) : null}
      {status.phase === 'ready' && status.facts.sessions.quarantined.length > 0 ? (
        <ul className={styles.incidentList}>
          {status.facts.sessions.quarantined.map((session) => (
            <li key={session.request_id}>
              Quarantined request <code>{session.request_id}</code> · <code>{session.reason}</code>
            </li>
          ))}
        </ul>
      ) : null}

      <p className={styles.hint}>
        A session counted above belongs to the request gateway's own session table; it is not a
        statement that a worker is healthy. Cold-restart status and ETA are unavailable in the
        current contracts, so none is shown. An evidence-feed reconnect is not proof of worker
        recovery.
      </p>

      <details className={styles.explorer}>
        <summary>Inspect the published route <span>Read-only · provenance and freshness included</span></summary>
        <RouteExplorerWorkspace />
      </details>
    </section>
  );
}
