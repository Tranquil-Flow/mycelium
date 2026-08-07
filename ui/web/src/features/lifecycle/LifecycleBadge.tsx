import type { LifecycleProjection } from './lifecycleProjection';

export interface LifecycleBadgeProps {
  readonly projection: LifecycleProjection;
}

export function LifecycleBadge({ projection }: LifecycleBadgeProps) {
  return (
    <span
      className="lifecycle-badge"
      role="status"
      aria-label={projection.accessibility_text}
      data-lifecycle-state={projection.state}
      data-route-ready={projection.route_ready ? 'true' : 'false'}
      data-inference-enabled={projection.inference_enabled ? 'true' : 'false'}
    >
      {projection.label}
    </span>
  );
}

export interface LifecycleCoveragePanelProps {
  readonly projections: readonly LifecycleProjection[];
}

function asRecordedFixtureProjection(projection: LifecycleProjection): LifecycleProjection {
  return {
    ...projection,
    route_ready: false,
    inference_enabled: false,
    qualifier_authority: false,
    accepted_request: null,
    qualified_binding: null,
    block_reason: projection.block_reason ?? 'recorded_fixture_not_authoritative',
  };
}

export function LifecycleCoveragePanel({ projections }: LifecycleCoveragePanelProps) {
  return (
    <section
      className="panel lifecycle-coverage-panel"
      role="region"
      aria-label="Recorded lifecycle projection coverage"
    >
      <div className="panel-titlebar compact">
        <div>
          <p className="panel-kicker">Recorded event projection lifecycle</p>
          <h3>Lifecycle status coverage</h3>
        </div>
        <span className="scope-badge">recorded_event_projection_only</span>
      </div>
      <p>
        Each state below is rendered as display-only lifecycle status. The UI keeps
        <code> real_device=false </code>
        and
        <code> physical_devices_present=0 </code>
        for every row.
      </p>
      <ul className="lifecycle-coverage-list">
        {projections.map((projection) => {
          const recordedProjection = asRecordedFixtureProjection(projection);
          return (
            <li
              key={recordedProjection.state}
              data-testid={`lifecycle-coverage-${recordedProjection.state}`}
              data-lifecycle-state={recordedProjection.state}
              data-route-ready="false"
              data-inference-enabled="false"
            >
              <LifecycleBadge projection={recordedProjection} />
              <span className="lifecycle-state-token">{recordedProjection.state}</span>
              <span>{recordedProjection.claim_boundary}</span>
              <span>real_device={String(recordedProjection.real_device)}</span>
              <span>physical_devices_present={recordedProjection.physical_devices_present}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
