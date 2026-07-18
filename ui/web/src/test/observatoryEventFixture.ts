export function validObservatoryAdapterEvent(generation = 1, sourceCursor = 3) {
  return {
    protocol: 'mycelium.observatory_stream.v1',
    generation,
    bundle: {
      snapshot: {
        protocol: 'mycelium.observatory.request_projection.v1',
        source_cursor: sourceCursor,
        observed_at_unix_ms: 1_000,
        qualification: {
          protocol: 'mycelium.route_qualification.v1',
          qualification_id: 'qualification-fixture',
          issued_at_unix_ms: 900,
          evidence_class: 'synthetic_test_fixture',
          route_ready: false,
          reason_codes: ['synthetic_test_fixture_not_accepted'],
          binding: {
            qualification_id: 'qualification-fixture',
            qualification_digest: `sha256:${'1'.repeat(64)}`,
            deployment_id: 'deployment-alpha',
            deployment_epoch: 7,
            topology_version: 11,
            model_id: 'model-alpha',
            resolved_commit: 'commit-abc123',
            manifest_digest: `sha256:${'2'.repeat(64)}`,
            path_manifest_digest: `sha256:${'3'.repeat(64)}`,
            stage_load_proof_digests: [`sha256:${'4'.repeat(64)}`],
          },
        },
        sessions: [
          {
            request_id: 'request-a',
            state: 'completed',
            last_sequence: 2,
            event_count: 3,
            token_count: 1,
            terminal: true,
            qualification_id: 'qualification-fixture',
            started_at_unix_ms: 950,
            updated_at_unix_ms: 990,
            quarantine_reason: null as string | null,
          },
        ],
      },
      incidents: [
        {
          protocol: 'mycelium.request_event.v1',
          source_cursor: 2,
          reason: 'cross_session_event',
        },
      ],
      provisioning: {
        protocol: 'mycelium.observatory.event_adapter_status.v1',
        route_ready: false,
        source_cursor: sourceCursor,
        buffered_sessions: 1,
        quarantine_capacity: 16,
        dropped_quarantine_count: 0,
      },
    },
  };
}
