import type { A5ReplicaTrackQualification } from './a5Replication';
import styles from './LiveRouteWorkspace.module.css';

export type A5ReplicaTrackView =
  | 'tracks'
  | 'qualification'
  | 'loss';

function short(value: string): string {
  return value.startsWith('sha256:')
    ? `${value.slice(0, 15)}…`
    : value;
}

export function A5ReplicaTrackPanel({
  qualifications,
  lossPlacementIds,
  nowUnixMs = Date.now(),
  view,
}: {
  readonly qualifications: readonly A5ReplicaTrackQualification[];
  readonly lossPlacementIds: readonly string[];
  readonly nowUnixMs?: number;
  readonly view: A5ReplicaTrackView;
}) {
  const lost = new Set(lossPlacementIds);
  const hasLostPlacement = (qualification: A5ReplicaTrackQualification) =>
    qualification.placement_ids.some((placementId) => lost.has(placementId));
  const isCurrent = (qualification: A5ReplicaTrackQualification) =>
    qualification.route_ready && qualification.issued_at_unix_ms <= nowUnixMs && qualification.expires_at_unix_ms > nowUnixMs;
  const state = (record: A5ReplicaTrackQualification) =>
    record.issued_at_unix_ms > nowUnixMs ? 'not yet valid'
      : record.expires_at_unix_ms <= nowUnixMs ? 'expired'
      : hasLostPlacement(record) ? 'placement lost'
      : record.route_ready ? 'qualified' : 'not qualified';
  const proof = (record: A5ReplicaTrackQualification, value: boolean) =>
    state(record) === 'qualified' ? value ? 'pass' : 'fail'
      : value ? 'recorded pass — not current' : 'not proven';
  const qualified = qualifications.filter(
    (qualification) => isCurrent(qualification),
  );
  const degraded = qualifications.filter(
    (qualification) =>
      isCurrent(qualification) && hasLostPlacement(qualification),
  );
  const surviving = qualified.filter(
    (qualification) => !hasLostPlacement(qualification),
  );

  return (
    <section
      className={styles.panel}
      aria-label={`${view} request-level stage replication`}
    >
      <div className={styles.panelTitlebar}>
        <div>
          <p className={styles.eyebrow}>Request-level stage replication</p>
          <h2>
            {view === 'tracks'
              ? 'Qualified replica tracks'
              : view === 'qualification'
                ? 'Replica qualification evidence'
                : 'Replica loss projection'}
          </h2>
        </div>
        <span className={styles.evidenceBadge}>data parallel</span>
      </div>
      <p>
        Each request is admitted onto exactly one complete legal track.
        Different requests can use different complete tracks; a single request is
        never split across replica placements and its KV stays pinned to the
        admitted track.
      </p>

      {view === 'tracks' ? (
        <div className={styles.tableWrap}>
          <table>
            <thead>
              <tr>
                <th>Track</th>
                <th>Placement</th>
                <th>Complete placement sequence</th>
                <th>Replica group</th>
                <th>Generation</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {qualifications.map((qualification) => (
                <tr key={qualification.qualification_id} data-qualification-id={qualification.qualification_id}>
                  <th scope="row">{short(qualification.track_id)}</th>
                  <td>{qualification.placement_id}</td>
                  <td>{qualification.placement_ids.join(' → ')}</td>
                  <td>{qualification.replica_group_id}</td>
                  <td>{qualification.qualifier_generation}</td>
                  <td>
                    {state(qualification) === 'not qualified' ? qualification.rejected_reasons.map((reason) => reason.replaceAll('_', ' ')).join(', ') || 'not qualified' : state(qualification)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {view === 'qualification' ? (
        <div className={styles.tableWrap}>
          <table>
            <thead>
              <tr>
                <th>Placement</th>
                <th>Parity</th>
                <th>Startup</th>
                <th>Memory</th>
                <th>Cleanup</th>
                <th>Link</th>
                <th>Evidence state</th>
                <th>Expiry</th>
              </tr>
            </thead>
            <tbody>
              {qualifications.map((qualification) => (
                <tr key={qualification.qualification_id} data-qualification-id={qualification.qualification_id}>
                  <th scope="row">{qualification.placement_id}</th>
                  <td>{proof(qualification, qualification.parity_verified)}</td>
                  <td>
                    {proof(qualification, qualification.startup_challenge_passed)}
                  </td>
                  <td>
                    {proof(qualification, qualification.memory_within_bounds)}
                  </td>
                  <td>
                    {proof(qualification, qualification.cleanup_within_bounds)}
                  </td>
                  <td>
                    {proof(qualification, qualification.directed_link_qualified)}
                  </td>
                  <td>{state(qualification)}</td>
                  <td>
                    {new Date(
                      qualification.expires_at_unix_ms,
                    ).toISOString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {view === 'loss' ? (
        <>
          <p>
            <strong>
              {`${surviving.length} surviving qualified track${surviving.length === 1 ? '' : 's'} · ${degraded.length} degraded by placement loss`}
            </strong>
            . New admission on a lost placement is rejected; only current surviving
            tracks can remain eligible at reduced capacity.
          </p>
          <div className={styles.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th>Placement</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {qualifications.map((qualification) => (
                  <tr key={qualification.qualification_id} data-qualification-id={qualification.qualification_id}>
                    <th scope="row">{qualification.placement_id}</th>
                    <td>
                      {state(qualification) === 'placement lost' ? 'lost — new admission blocked' : state(qualification) === 'qualified' ? 'surviving' : state(qualification)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}

      <p>
        <small>
          Planner plan never reports route-ready; only the qualifier&apos;s
          replica qualification carries route-ready for a replica track.
          Benchmarks do not promote a claim.
        </small>
      </p>
    </section>
  );
}
