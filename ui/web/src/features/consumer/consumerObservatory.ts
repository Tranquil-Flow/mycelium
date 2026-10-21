// SPDX-License-Identifier: AGPL-3.0-only
import { backgroundFetch } from '../../app/backgroundFetch';
import {
  decodeObservatoryAdapterEvent,
  type ObservatoryAdapterBundle,
  type ObservatoryRequestState,
} from '../../data/observatoryEventProjection';

/** The product origin serves this read-only route-status document. */
export const CONSUMER_ROUTE_STATUS_PATH = '/api/v1/observatory/snapshot' as const;
const MAX_ROUTE_STATUS_UTF8_BYTES = 1_048_576;

export type ConsumerRouteStatusFetch = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export class ConsumerRouteStatusError extends Error {
  constructor(
    readonly code: string,
    readonly retryable: boolean,
    readonly status: number | null = null,
  ) {
    super(code);
    this.name = 'ConsumerRouteStatusError';
  }
}

export interface ConsumerRouteStatusSessionSummary {
  readonly total: number;
  readonly by_state: Readonly<Record<ObservatoryRequestState, number>>;
  readonly failed_request_ids: readonly string[];
  readonly cancelled_request_ids: readonly string[];
  readonly quarantined: readonly { readonly request_id: string; readonly reason: string }[];
}

export interface ConsumerRouteStatusFacts {
  readonly generation: number;
  readonly source_cursor: number;
  readonly observed_at_unix_ms: number;
  readonly route_ready: boolean;
  readonly buffered_sessions: number;
  readonly dropped_quarantine_count: number;
  readonly quarantine_capacity: number;
  readonly sessions: ConsumerRouteStatusSessionSummary;
  readonly incidents: readonly {
    readonly protocol: string;
    readonly reason: string;
    readonly source_cursor: number;
  }[];
  readonly qualification: {
    readonly qualification_id: string;
    readonly evidence_class: string;
    readonly route_ready: boolean;
    readonly reason_codes: readonly string[];
    readonly model_id: string;
    readonly deployment_id: string;
    readonly deployment_epoch: number;
    readonly topology_version: number;
    readonly resolved_commit: string;
  } | null;
}

/** The three honest states of the route-status read: loading, read, or named failure. */
export type ConsumerRouteStatusState =
  | { readonly phase: 'loading' }
  | { readonly phase: 'ready'; readonly facts: ConsumerRouteStatusFacts }
  | { readonly phase: 'unavailable'; readonly code: string; readonly detail: string };

const REQUEST_STATES: readonly ObservatoryRequestState[] = Object.freeze([
  'accepted',
  'streaming',
  'completed',
  'cancelled',
  'failed',
  'quarantined',
]);

function summarizeSessions(
  bundle: ObservatoryAdapterBundle,
): ConsumerRouteStatusSessionSummary {
  const counts: Record<ObservatoryRequestState, number> = {
    accepted: 0,
    streaming: 0,
    completed: 0,
    cancelled: 0,
    failed: 0,
    quarantined: 0,
  };
  const failed: string[] = [];
  const cancelled: string[] = [];
  const quarantined: { request_id: string; reason: string }[] = [];
  for (const session of bundle.snapshot.sessions) {
    if (!REQUEST_STATES.includes(session.state)) continue;
    counts[session.state] += 1;
    if (session.state === 'failed') failed.push(session.request_id);
    if (session.state === 'cancelled') cancelled.push(session.request_id);
    if (session.state === 'quarantined' && session.quarantine_reason !== null) {
      quarantined.push({ request_id: session.request_id, reason: session.quarantine_reason });
    }
  }
  return Object.freeze({
    total: bundle.snapshot.sessions.length,
    by_state: Object.freeze(counts),
    failed_request_ids: Object.freeze(failed),
    cancelled_request_ids: Object.freeze(cancelled),
    quarantined: Object.freeze(quarantined),
  });
}

export function projectConsumerRouteStatus(
  bundle: ObservatoryAdapterBundle,
  generation: number,
): ConsumerRouteStatusFacts {
  const qualification = bundle.snapshot.qualification;
  return Object.freeze({
    generation,
    source_cursor: bundle.snapshot.source_cursor,
    observed_at_unix_ms: bundle.snapshot.observed_at_unix_ms,
    route_ready: bundle.provisioning.route_ready,
    buffered_sessions: bundle.provisioning.buffered_sessions,
    dropped_quarantine_count: bundle.provisioning.dropped_quarantine_count,
    quarantine_capacity: bundle.provisioning.quarantine_capacity,
    sessions: summarizeSessions(bundle),
    incidents: Object.freeze(
      bundle.incidents.map((incident) =>
        Object.freeze({
          protocol: incident.protocol,
          reason: incident.reason,
          source_cursor: incident.source_cursor,
        }),
      ),
    ),
    qualification:
      qualification === null
        ? null
        : Object.freeze({
            qualification_id: qualification.qualification_id,
            evidence_class: qualification.evidence_class,
            route_ready: qualification.route_ready,
            reason_codes: Object.freeze([...qualification.reason_codes]),
            model_id: qualification.binding.model_id,
            deployment_id: qualification.binding.deployment_id,
            deployment_epoch: qualification.binding.deployment_epoch,
            topology_version: qualification.binding.topology_version,
            resolved_commit: qualification.binding.resolved_commit,
          }),
  });
}

/**
 * Reads the real route-status document from the product origin.
 *
 * Failures are typed and returned to the caller; nothing is substituted for a
 * missing document. The decode path is the same validated decoder the operator
 * Observatory stream uses, so a malformed or unqualified document is rejected
 * rather than rendered.
 */
export class ConsumerRouteStatusClient {
  constructor(
    private readonly fetchImplementation: ConsumerRouteStatusFetch = backgroundFetch,
  ) {}

  async load(signal?: AbortSignal): Promise<ConsumerRouteStatusFacts> {
    let response: Response;
    try {
      response = await this.fetchImplementation(CONSUMER_ROUTE_STATUS_PATH, {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        headers: { Accept: 'application/json' },
        signal,
      });
    } catch {
      throw new ConsumerRouteStatusError('route_status_unreachable', true);
    }
    if (!response.ok) {
      throw new ConsumerRouteStatusError(
        'route_status_unavailable',
        response.status >= 500,
        response.status,
      );
    }
    const contentType = response.headers.get('Content-Type')?.toLowerCase() ?? '';
    if (!contentType.startsWith('application/json')) {
      throw new ConsumerRouteStatusError('invalid_route_status_content_type', false, response.status);
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > MAX_ROUTE_STATUS_UTF8_BYTES) {
      throw new ConsumerRouteStatusError('route_status_too_large', false, response.status);
    }
    let payload: unknown;
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      throw new ConsumerRouteStatusError('invalid_route_status_document', false, response.status);
    }
    try {
      const event = decodeObservatoryAdapterEvent(payload);
      return projectConsumerRouteStatus(event.bundle, event.generation);
    } catch {
      throw new ConsumerRouteStatusError('invalid_route_status_document', false, response.status);
    }
  }
}
