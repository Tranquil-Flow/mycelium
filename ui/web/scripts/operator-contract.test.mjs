import assert from 'node:assert/strict';
import test from 'node:test';

import {
  decodeEvidenceRecord,
  decodeOperatorStatus,
} from '../../../mycelium_interactive/static/operatorContract.js';

const record = {
  protocol: 'mycelium.interactive_inference_record.v1',
  request_id: 'request-test',
  prompt_digest: `sha256:${'a'.repeat(64)}`,
  prompt_bytes: 12,
  initial_tokens: [1, 2],
  generated_tokens: [4, 5],
  generated_labels: ['moon', 'swarm'],
  required_distinct_peers: 2,
  observed_distinct_peers: 2,
  peer_ids: ['peer-a', 'peer-b'],
  token_records: [
    {
      token_index: 0,
      stage_request_id: 'request-test:token-0',
      browser_peer_id: 'peer-a',
      browser_job_id: 'job-a',
      browser_output_digest: `sha256:${'b'.repeat(64)}`,
      selected_token: 4,
      selected_label: 'moon',
      context_length: 3,
      intermediate_error: 1e-7,
      logit_error: 2e-7,
      route_ready: false,
    },
    {
      token_index: 1,
      stage_request_id: 'request-test:token-1',
      browser_peer_id: 'peer-b',
      browser_job_id: 'job-b',
      browser_output_digest: `sha256:${'c'.repeat(64)}`,
      selected_token: 5,
      selected_label: 'swarm',
      context_length: 4,
      intermediate_error: 1e-8,
      logit_error: 2e-8,
      route_ready: false,
    },
  ],
  stage_pack_digest: `sha256:${'d'.repeat(64)}`,
  created_at: 1_800_000_000,
  completed_at: 1_800_000_001,
  max_intermediate_error: 1e-7,
  max_logit_error: 2e-7,
  route_ready: false,
  local_evidence_only: true,
};

const status = {
  run_id: 'run-test',
  peer_count: 2,
  ready_peer_count: 2,
  peers: [],
  recent_requests: [record],
  route_ready: false,
  local_evidence_only: true,
};

test('accepts bounded local evidence and status', () => {
  assert.equal(decodeEvidenceRecord(record).request_id, 'request-test');
  assert.equal(decodeOperatorStatus(status).recent_requests.length, 1);
});

test('rejects promoted or non-local operator evidence', () => {
  assert.throws(
    () => decodeEvidenceRecord({ ...record, route_ready: true }),
    /interactive_evidence_boundary_invalid/,
  );
  assert.throws(
    () => decodeOperatorStatus({ ...status, local_evidence_only: false }),
    /interactive_status_boundary_invalid/,
  );
});

test('rejects malformed exact-N contributors in status and direct records', () => {
  const missingContributor = {
    ...record,
    token_records: [
      record.token_records[0],
      { ...record.token_records[1], browser_peer_id: 'peer-a' },
    ],
  };
  assert.throws(
    () => decodeEvidenceRecord(missingContributor),
    /interactive_evidence_invalid/,
  );
  assert.throws(
    () => decodeOperatorStatus({ ...status, recent_requests: [missingContributor] }),
    /interactive_evidence_invalid/,
  );
});
