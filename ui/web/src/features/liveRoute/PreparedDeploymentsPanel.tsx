import type { DeploymentActivationStatus, PreparedDeploymentCandidate } from './deploymentActivation';
import styles from './LiveRouteWorkspace.module.css';

export type PreparedDeploymentsView = 'inference' | 'plans' | 'readiness' | 'incidents' | 'nodes';

function phase(value: PreparedDeploymentCandidate): string {
  if (value.phase === 'validating_plan') return 'Validating the prepared route';
  if (value.phase === 'opening_route') return 'Opening and loading the physical stages';
  if (value.phase === 'qualifying_route') return 'Running the distributed startup challenge';
  if (value.phase === 'registering') return 'Adding the qualified deployment to model selection';
  if (value.state === 'prepared') return 'Ready for operator activation';
  if (value.state === 'qualified') return 'Qualified and available for selection';
  if (value.state === 'active') return 'Active for new inference requests';
  if (value.state === 'unavailable') return 'The registered route is unavailable';
  return 'Activation failed; the current model was not changed';
}

function reason(value: string | null): string {
  if (value === null) return 'None';
  if (value === 'startup_challenge_failed') return 'The distributed startup challenge did not match';
  if (value === 'member_in_active_route') return 'A required member is already committed to an incompatible active route';
  if (value === 'activation_busy') return 'Another deployment is currently activating';
  if (value === 'route_unavailable') return 'The registered physical route is no longer alive';
  if (value === 'activation_stopping') return 'The live service is shutting down';
  return value.replaceAll('_', ' ');
}

export function PreparedDeploymentsPanel({
  status,
  view,
  activatingCandidateId,
  error,
  onActivate,
}: {
  readonly status: DeploymentActivationStatus;
  readonly view: PreparedDeploymentsView;
  readonly activatingCandidateId: string | null;
  readonly error: string | null;
  readonly onActivate: (candidateId: string) => void;
}) {
  return (
    <section className={styles.panel} aria-labelledby={`prepared-deployments-${view}`}>
      <div className={styles.panelTitlebar}>
        <div>
          <p className={styles.eyebrow}>Operator-prepared local routes</p>
          <h2 id={`prepared-deployments-${view}`}>Prepared deployments</h2>
        </div>
        <span className={styles.evidenceBadge}>{status.busy_candidate_id === null ? 'idle' : 'activating'}</span>
      </div>
      <p>
        These routes come from the private operator candidate directory. Activation opens the physical stages and runs
        qualification; it never downloads a model or changes the selected model automatically.
      </p>
      {status.invalid_candidate_count > 0 ? (
        <p role="alert">{status.invalid_candidate_count} unsafe or invalid candidate {status.invalid_candidate_count === 1 ? 'file was' : 'files were'} rejected.</p>
      ) : null}
      {status.candidates.length === 0 ? <p>No valid prepared deployment is waiting.</p> : (
        <div className={styles.tableWrap}><table>
          <thead><tr><th>Model</th><th>Route</th><th>State and progress</th><th>Result</th><th>Action</th></tr></thead>
          <tbody>{status.candidates.map((candidate) => {
            const canActivate = candidate.state === 'prepared' || candidate.state === 'failed';
            return <tr key={candidate.candidate_id}>
              <th scope="row">{candidate.model_id}<small> · {candidate.model_revision.slice(0, 8)} · {candidate.quantization}</small></th>
              <td>{candidate.topology_size} physical {candidate.topology_size === 1 ? 'stage' : 'stages'}<small> · deployment {candidate.deployment_id}</small></td>
              <td>
                <strong>{candidate.state}</strong><small> · {phase(candidate)}</small>
                <progress value={candidate.completed_steps} max={candidate.total_steps} aria-label={`${candidate.model_id} activation progress`} />
              </td>
              <td>{reason(candidate.reason_code)}</td>
              <td>{canActivate ? (
                <button
                  type="button"
                  disabled={status.busy_candidate_id !== null || activatingCandidateId !== null}
                  onClick={() => onActivate(candidate.candidate_id)}
                >{activatingCandidateId === candidate.candidate_id ? 'Starting…' : candidate.state === 'failed' ? 'Retry activation' : 'Activate deployment'}</button>
              ) : candidate.state === 'qualified' ? 'Select it in Inference' : candidate.state === 'active' ? 'Currently selected' : candidate.state === 'activating' ? 'Activation in progress' : 'Unavailable'}</td>
            </tr>;
          })}</tbody>
        </table></div>
      )}
      {error === null ? null : <p role="alert">Activation request failed: {reason(error)}</p>}
    </section>
  );
}
