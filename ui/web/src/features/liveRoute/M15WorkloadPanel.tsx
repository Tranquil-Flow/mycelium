import type { M15PlanComparison } from './m15Comparison';
import styles from './LiveRouteWorkspace.module.css';

function number(value: number, digits = 1): string { return value.toFixed(digits); }

export function M15WorkloadPanel({ comparison }: { readonly comparison: M15PlanComparison }) {
  return (
    <section className={styles.panel} aria-label="M15 workload-aware plan comparison">
      <h2>Workload-aware plan frontier</h2>
      <p><strong>Modeled planner intent</strong> · {comparison.calibration_state.replaceAll('_', ' ')} · never runtime readiness</p>
      {comparison.comparisons.map((matrix) => {
        const profile = comparison.profiles.find((item) => item.profile_id === matrix.profile_id)!;
        const observation = comparison.observations.find((item) => item.profile_id === matrix.profile_id);
        return (
          <article key={matrix.profile_id}>
            <h3>{matrix.profile_id}</h3>
            <p>
              {profile.scenarios[0].qos_class} QoS · batch {profile.scenarios[0].batch_size} · content-free trace {profile.trace_sample_count.toLocaleString()} samples
            </p>
            <p><strong>Robust winner:</strong> {matrix.selected_candidate_id} · worst case {matrix.winning_scenario_id} / {matrix.winning_metric}</p>
            <p><strong>Pareto frontier:</strong> {matrix.pareto_candidate_ids.join(', ')}</p>
            {observation === undefined ? (
              <p><strong>Physical calibration:</strong> not observed.</p>
            ) : (
              <div>
                <p><strong>Physical calibration:</strong> {observation.overall_state} · request {observation.request_id} · topology {observation.topology_version} · {observation.context_tokens} context / {observation.output_tokens} output tokens</p>
                <div className={styles.tableWrap}>
                  <table aria-label={`${matrix.profile_id} modeled versus observed calibration`}>
                    <thead><tr><th>Metric</th><th>Exact-shape prediction</th><th>Physical observation</th><th>Signed error</th><th>Absolute relative error</th><th>Budget</th></tr></thead>
                    <tbody>
                      <tr><th scope="row">TTFT</th><td>{number(observation.prediction.ttft_ms as number)} ms modeled</td><td>{number(observation.observed.ttft_ms)} ms observed</td><td>{number(observation.signed_error.ttft_ms)} ms</td><td>{number(observation.absolute_relative_error.ttft * 100)}%</td><td>{observation.budget_results.model_ttft}</td></tr>
                      <tr><th scope="row">TPOT</th><td>{number(observation.prediction.tpot_ms as number)} ms modeled</td><td>{number(observation.observed.tpot_ms)} ms observed</td><td>{number(observation.signed_error.tpot_ms)} ms</td><td>{number(observation.absolute_relative_error.tpot * 100)}%</td><td>{observation.budget_results.model_tpot}</td></tr>
                      <tr><th scope="row">Goodput</th><td>{number(observation.prediction.output_goodput_tps as number, 2)} tok/s modeled</td><td>{number(observation.observed.output_goodput_tps, 2)} tok/s observed</td><td>{number(observation.signed_error.output_goodput_tps, 2)} tok/s</td><td>{number(observation.absolute_relative_error.throughput * 100)}%</td><td>{observation.budget_results.model_throughput}</td></tr>
                    </tbody>
                  </table>
                </div>
                <p><strong>Frame counters:</strong> {observation.counters_before.frames_sent}/{observation.counters_before.frames_received} → {observation.counters_after.frames_sent}/{observation.counters_after.frames_received} sent/received.</p>
                <p><strong>Approved exclusions:</strong> peak memory, energy/thermal, reconnect. <strong>M16 boundary:</strong> admission latency, batch shape, concurrency, queueing.</p>
              </div>
            )}
            <div className={styles.tableWrap}>
              <table aria-label={`${matrix.profile_id} policy comparison`}>
                <thead><tr><th>Policy</th><th>Allocation</th><th>TTFT</th><th>TPOT</th><th>Goodput</th><th>Decision</th></tr></thead>
                <tbody>{matrix.candidates.map((candidate) => {
                  const scenario = candidate.scenarios[0];
                  return <tr key={candidate.candidate_id}><th scope="row">{candidate.policy_id}</th><td>{candidate.allocation.map((item) => `${item.node_id} [${item.start},${item.end})`).join(' → ')}</td><td>{number(scenario.ttft_ms)} ms modeled</td><td>{number(scenario.tpot_ms)} ms modeled</td><td>{number(scenario.output_goodput_tps, 2)} tok/s modeled</td><td>{candidate.selected ? 'robust winner' : candidate.pareto ? 'Pareto alternative' : 'dominated'}</td></tr>;
                })}</tbody>
              </table>
            </div>
          </article>
        );
      })}
      <p><strong>Deferred to M16:</strong> {comparison.deferred_to_m16.join(', ')}.</p>
    </section>
  );
}
