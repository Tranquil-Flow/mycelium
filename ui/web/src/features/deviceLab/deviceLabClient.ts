export interface DeviceLabPeer {
  readonly peer_id: string;
  readonly state: string;
  readonly completed_jobs: number;
  readonly assigned_layer: {
    readonly start_layer: number;
    readonly end_layer_exclusive: number;
  };
  readonly pack_digest: string;
}

export interface DeviceLabTokenRecord {
  readonly token_index: number;
  readonly stage_request_id: string;
  readonly browser_peer_id: string;
  readonly browser_job_id: string;
  readonly browser_output_digest: string;
  readonly selected_token: number;
  readonly selected_label: string;
  readonly context_length: number;
  readonly intermediate_error: number;
  readonly logit_error: number;
  readonly route_ready: false;
}

export interface DeviceLabInferenceRecord {
  readonly protocol: 'mycelium.interactive_inference_record.v1';
  readonly request_id: string;
  readonly prompt_digest: string;
  readonly prompt_bytes: number;
  readonly initial_tokens: readonly number[];
  readonly generated_tokens: readonly number[];
  readonly generated_labels: readonly string[];
  readonly max_intermediate_error: number;
  readonly max_logit_error: number;
  readonly peer_ids: readonly string[];
  readonly required_distinct_peers: number;
  readonly observed_distinct_peers: number;
  readonly stage_pack_digest: string;
  readonly token_records: readonly DeviceLabTokenRecord[];
  readonly created_at: number;
  readonly completed_at: number;
  readonly route_ready: false;
  readonly local_evidence_only: true;
}

export interface DeviceLabStatus {
  readonly protocol: string;
  readonly interactive_protocol: string;
  readonly run_id: string;
  readonly local_evidence_only: true;
  readonly route_ready: false;
  readonly peer_count: number;
  readonly ready_peer_count: number;
  readonly pending_job_count: number;
  readonly active_request_count: number;
  readonly completed_request_count: number;
  readonly stage_pack_digest: string;
  readonly vocabulary: readonly string[];
  readonly peers: readonly DeviceLabPeer[];
  readonly recent_requests: readonly DeviceLabInferenceRecord[];
}

export interface DeviceLabInvite {
  readonly url: string;
  readonly expires_at: number;
  /** Validated as false at the trust boundary; widened for ergonomic test doubles. */
  readonly route_ready: boolean;
}

export interface DeviceLabSubmission {
  readonly prompt: string;
  readonly max_new_tokens: number;
  readonly required_distinct_peers: number;
  readonly request_id: string;
}

export interface DeviceLabClient {
  status(signal: AbortSignal): Promise<DeviceLabStatus>;
  createInvite(ttlSeconds: number, signal: AbortSignal): Promise<DeviceLabInvite>;
  infer(submission: DeviceLabSubmission, signal: AbortSignal): Promise<DeviceLabInferenceRecord>;
  cancel(requestId: string, signal: AbortSignal): Promise<boolean>;
  request(requestId: string, signal: AbortSignal): Promise<DeviceLabInferenceRecord>;
}

export class DeviceLabClientError extends Error {
  constructor(
    readonly code: string,
    readonly status: number | null = null,
    readonly retryable = false,
  ) {
    super(code);
    this.name = 'DeviceLabClientError';
  }
}

type JsonRecord = Record<string, unknown>;

function record(value: unknown, code = 'device_lab_contract_invalid'): JsonRecord {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new DeviceLabClientError(code);
  }
  return value as JsonRecord;
}

function requireBoundary(value: JsonRecord): void {
  if (value.route_ready !== false || value.local_evidence_only !== true) {
    throw new DeviceLabClientError('invalid_device_lab_contract');
  }
}

function decodeInference(value: unknown): DeviceLabInferenceRecord {
  const inference = record(value, 'device_lab_record_invalid');
  requireBoundary(inference);
  const generatedTokens = inference.generated_tokens;
  const generatedLabels = inference.generated_labels;
  const peerIds = inference.peer_ids;
  const tokenRecords = inference.token_records;
  const requiredPeers = (
    typeof inference.required_distinct_peers === 'number'
    && Number.isInteger(inference.required_distinct_peers)
  ) ? inference.required_distinct_peers : null;
  if (
    inference.protocol !== 'mycelium.interactive_inference_record.v1'
    || typeof inference.request_id !== 'string'
    || typeof inference.prompt_digest !== 'string'
    || typeof inference.prompt_bytes !== 'number'
    || !Array.isArray(inference.initial_tokens)
    || !Array.isArray(generatedTokens)
    || !Array.isArray(generatedLabels)
    || !Array.isArray(peerIds)
    || !Array.isArray(tokenRecords)
    || requiredPeers === null
    || requiredPeers < 1
    || inference.observed_distinct_peers !== requiredPeers
    || peerIds.length !== requiredPeers
    || !peerIds.every((peerId) => typeof peerId === 'string' && peerId.length > 0)
    || new Set(peerIds).size !== requiredPeers
    || generatedLabels.length !== generatedTokens.length
    || tokenRecords.length !== generatedTokens.length
    || generatedTokens.length < requiredPeers
    || !generatedTokens.every((token) => typeof token === 'number' && Number.isInteger(token))
    || !generatedLabels.every((label) => typeof label === 'string')
    || typeof inference.stage_pack_digest !== 'string'
    || typeof inference.created_at !== 'number'
    || !Number.isFinite(inference.created_at)
    || typeof inference.completed_at !== 'number'
    || !Number.isFinite(inference.completed_at)
    || typeof inference.max_intermediate_error !== 'number'
    || !Number.isFinite(inference.max_intermediate_error)
    || typeof inference.max_logit_error !== 'number'
    || !Number.isFinite(inference.max_logit_error)
  ) {
    throw new DeviceLabClientError('device_lab_record_invalid');
  }
  const cohort = new Set(peerIds as string[]);
  const observedContributors = new Set<string>();
  for (const [index, rawTokenRecord] of tokenRecords.entries()) {
    const token = record(rawTokenRecord, 'device_lab_record_invalid');
    if (
      token.token_index !== index
      || typeof token.stage_request_id !== 'string'
      || typeof token.browser_peer_id !== 'string'
      || !cohort.has(token.browser_peer_id)
      || typeof token.browser_job_id !== 'string'
      || typeof token.browser_output_digest !== 'string'
      || token.selected_token !== generatedTokens[index]
      || token.selected_label !== generatedLabels[index]
      || typeof token.context_length !== 'number'
      || !Number.isInteger(token.context_length)
      || typeof token.intermediate_error !== 'number'
      || !Number.isFinite(token.intermediate_error)
      || typeof token.logit_error !== 'number'
      || !Number.isFinite(token.logit_error)
      || token.route_ready !== false
    ) {
      throw new DeviceLabClientError('device_lab_record_invalid');
    }
    observedContributors.add(token.browser_peer_id as string);
  }
  if (observedContributors.size !== requiredPeers) {
    throw new DeviceLabClientError('device_lab_record_invalid');
  }
  return inference as unknown as DeviceLabInferenceRecord;
}

function decodeStatus(value: unknown): DeviceLabStatus {
  const envelope = record(value);
  if (envelope.ok !== true) throw new DeviceLabClientError('device_lab_status_invalid');
  const status = record(envelope.status);
  requireBoundary(status);
  if (
    typeof status.run_id !== 'string'
    || typeof status.peer_count !== 'number'
    || typeof status.ready_peer_count !== 'number'
    || !Array.isArray(status.peers)
    || !Array.isArray(status.recent_requests)
  ) {
    throw new DeviceLabClientError('device_lab_status_invalid');
  }
  return {
    ...status,
    recent_requests: status.recent_requests.map(decodeInference),
  } as unknown as DeviceLabStatus;
}

function decodeRecord(value: unknown): DeviceLabInferenceRecord {
  const envelope = record(value);
  if (envelope.ok !== true) throw new DeviceLabClientError('device_lab_record_invalid');
  return decodeInference(envelope.record);
}

function decodeInvite(value: unknown): DeviceLabInvite {
  const envelope = record(value);
  if (envelope.ok !== true) throw new DeviceLabClientError('device_lab_invite_invalid');
  const invite = record(envelope.invite);
  if (
    typeof invite.url !== 'string'
    || typeof invite.expires_at !== 'number'
    || invite.route_ready !== false
  ) {
    throw new DeviceLabClientError('device_lab_invite_invalid');
  }
  return invite as unknown as DeviceLabInvite;
}

function decodeCancel(value: unknown): boolean {
  const envelope = record(value);
  if (envelope.ok !== true || typeof envelope.cancelled !== 'boolean' || envelope.route_ready !== false) {
    throw new DeviceLabClientError('device_lab_cancel_invalid');
  }
  return envelope.cancelled;
}

export class HttpDeviceLabClient implements DeviceLabClient {
  constructor(
    private readonly operatorToken: string,
    private readonly fetcher: typeof fetch = fetch,
  ) {
    if (operatorToken.length === 0) {
      throw new DeviceLabClientError('operator_capability_missing');
    }
  }

  status(signal?: AbortSignal): Promise<DeviceLabStatus> {
    return this.get('/api/interactive/status', this.signal(signal)).then(decodeStatus);
  }

  createInvite(ttlSeconds: number, signal?: AbortSignal): Promise<DeviceLabInvite> {
    return this.post('/api/interactive/invite', { ttl_seconds: ttlSeconds }, this.signal(signal)).then(decodeInvite);
  }

  infer(submission: DeviceLabSubmission, signal?: AbortSignal): Promise<DeviceLabInferenceRecord> {
    return this.post('/api/interactive/infer', submission, this.signal(signal)).then(decodeRecord);
  }

  cancel(requestId: string, signal?: AbortSignal): Promise<boolean> {
    return this.post('/api/interactive/cancel', { request_id: requestId }, this.signal(signal)).then(decodeCancel);
  }

  request(requestId: string, signal?: AbortSignal): Promise<DeviceLabInferenceRecord> {
    if (!/^[A-Za-z0-9._:-]{1,512}$/.test(requestId)) {
      return Promise.reject(new DeviceLabClientError('request_id_invalid'));
    }
    return this.get(
      `/api/interactive/requests/${encodeURIComponent(requestId)}`,
      this.signal(signal),
    ).then(decodeRecord);
  }

  private signal(signal?: AbortSignal): AbortSignal {
    return signal ?? new AbortController().signal;
  }

  private async get(path: string, signal: AbortSignal): Promise<unknown> {
    return this.send(path, { method: 'GET', signal });
  }

  private async post(path: string, body: unknown, signal: AbortSignal): Promise<unknown> {
    return this.send(path, {
      method: 'POST',
      signal,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  private async send(path: string, init: RequestInit): Promise<unknown> {
    const headers = new Headers(init.headers);
    headers.set('accept', 'application/json');
    headers.set('authorization', `Bearer ${this.operatorToken}`);
    let response: Response;
    try {
      response = await this.fetcher.call(globalThis, path, {
        ...init,
        headers,
        cache: 'no-store',
        credentials: 'same-origin',
        redirect: 'error',
        referrerPolicy: 'no-referrer',
      });
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') throw reason;
      throw new DeviceLabClientError('device_lab_network_unavailable');
    }
    let document: unknown;
    try {
      document = await response.json();
    } catch {
      throw new DeviceLabClientError('device_lab_json_invalid', response.status);
    }
    if (!response.ok) {
      const envelope = record(document, 'device_lab_request_failed');
      throw new DeviceLabClientError(
        typeof envelope.error === 'string' ? envelope.error : 'device_lab_request_failed',
        response.status,
      );
    }
    return document;
  }
}

export interface OperatorDeviceLabClientOptions {
  readonly operatorToken: string;
  readonly fetcher?: typeof fetch;
}

/** Named compatibility surface for operator-facing call sites and direct contract tests. */
export class OperatorDeviceLabClient extends HttpDeviceLabClient {
  constructor({ operatorToken, fetcher = fetch }: OperatorDeviceLabClientOptions) {
    super(operatorToken, fetcher);
  }
}
