function object(value, code) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(code);
  }
  return value;
}

function requireBoundary(value, code) {
  if (value.route_ready !== false || value.local_evidence_only !== true) {
    throw new Error(code);
  }
}

function finiteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

export function decodeEvidenceRecord(value) {
  const evidence = object(value, 'interactive_evidence_invalid');
  requireBoundary(evidence, 'interactive_evidence_boundary_invalid');

  const requiredPeers = evidence.required_distinct_peers;
  const peerIds = evidence.peer_ids;
  const generatedTokens = evidence.generated_tokens;
  const generatedLabels = evidence.generated_labels;
  const tokenRecords = evidence.token_records;
  if (
    evidence.protocol !== 'mycelium.interactive_inference_record.v1'
    || typeof evidence.request_id !== 'string'
    || evidence.request_id.length === 0
    || typeof evidence.prompt_digest !== 'string'
    || !finiteNumber(evidence.prompt_bytes)
    || !Array.isArray(evidence.initial_tokens)
    || !Array.isArray(generatedTokens)
    || !Array.isArray(generatedLabels)
    || !Array.isArray(peerIds)
    || !Array.isArray(tokenRecords)
    || !Number.isInteger(requiredPeers)
    || requiredPeers < 1
    || evidence.observed_distinct_peers !== requiredPeers
    || peerIds.length !== requiredPeers
    || !peerIds.every((peerId) => typeof peerId === 'string' && peerId.length > 0)
    || new Set(peerIds).size !== requiredPeers
    || generatedTokens.length < requiredPeers
    || generatedLabels.length !== generatedTokens.length
    || tokenRecords.length !== generatedTokens.length
    || !generatedTokens.every(Number.isInteger)
    || !generatedLabels.every((label) => typeof label === 'string')
    || typeof evidence.stage_pack_digest !== 'string'
    || !finiteNumber(evidence.created_at)
    || !finiteNumber(evidence.completed_at)
    || !finiteNumber(evidence.max_intermediate_error)
    || !finiteNumber(evidence.max_logit_error)
  ) {
    throw new Error('interactive_evidence_invalid');
  }

  const cohort = new Set(peerIds);
  const contributors = new Set();
  tokenRecords.forEach((rawToken, index) => {
    const token = object(rawToken, 'interactive_evidence_invalid');
    if (
      token.token_index !== index
      || typeof token.stage_request_id !== 'string'
      || typeof token.browser_peer_id !== 'string'
      || !cohort.has(token.browser_peer_id)
      || typeof token.browser_job_id !== 'string'
      || typeof token.browser_output_digest !== 'string'
      || token.selected_token !== generatedTokens[index]
      || token.selected_label !== generatedLabels[index]
      || !Number.isInteger(token.context_length)
      || !finiteNumber(token.intermediate_error)
      || !finiteNumber(token.logit_error)
      || token.route_ready !== false
    ) {
      throw new Error('interactive_evidence_invalid');
    }
    contributors.add(token.browser_peer_id);
  });
  if (contributors.size !== requiredPeers) {
    throw new Error('interactive_evidence_invalid');
  }
  return evidence;
}

export function decodeOperatorStatus(value) {
  const status = object(value, 'interactive_status_invalid');
  requireBoundary(status, 'interactive_status_boundary_invalid');
  if (
    typeof status.run_id !== 'string'
    || !Number.isInteger(status.peer_count)
    || status.peer_count < 0
    || !Number.isInteger(status.ready_peer_count)
    || status.ready_peer_count < 0
    || status.ready_peer_count > status.peer_count
    || !Array.isArray(status.peers)
    || !Array.isArray(status.recent_requests)
  ) {
    throw new Error('interactive_status_invalid');
  }
  return {
    ...status,
    recent_requests: status.recent_requests.map(decodeEvidenceRecord),
  };
}
