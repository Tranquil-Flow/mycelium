// SPDX-License-Identifier: AGPL-3.0-only
import { describe, expect, it } from 'vitest';
import { validObservatoryAdapterEvent } from '../../test/observatoryEventFixture';
import {
  CONSUMER_ROUTE_STATUS_PATH,
  ConsumerRouteStatusClient,
  ConsumerRouteStatusError,
  projectConsumerRouteStatus,
} from './consumerObservatory';
import { decodeObservatoryAdapterEvent } from '../../data/observatoryEventProjection';

function response(
  document: unknown,
  contentType = 'application/json',
  status = 200,
): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) => (name.toLowerCase() === 'content-type' ? contentType : null),
    },
    text: async () => (typeof document === 'string' ? document : JSON.stringify(document)),
    url: '',
    redirected: false,
  } as unknown as Response;
}

describe('consumer route-status client', () => {
  it('reads the real same-origin route-status document and projects only published facts', async () => {
    const calls: { path: string; init: RequestInit | undefined }[] = [];
    const client = new ConsumerRouteStatusClient(async (path, init) => {
      calls.push({ path: String(path), init });
      return response(validObservatoryAdapterEvent(7, 9));
    });
    const facts = await client.load();

    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe(CONSUMER_ROUTE_STATUS_PATH);
    expect(calls[0].init).toMatchObject({
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
    });
    expect(facts.generation).toBe(7);
    expect(facts.source_cursor).toBe(9);
    expect(facts.route_ready).toBe(false);
    expect(facts.sessions.total).toBe(1);
    expect(facts.sessions.by_state.completed).toBe(1);
    expect(facts.sessions.failed_request_ids).toEqual([]);
    expect(facts.incidents).toEqual([
      { protocol: 'mycelium.request_event.v1', reason: 'cross_session_event', source_cursor: 2 },
    ]);
    expect(facts.qualification?.qualification_id).toBe('qualification-fixture');
    expect(facts.qualification?.evidence_class).toBe('synthetic_test_fixture');
    expect(facts.qualification?.route_ready).toBe(false);
    expect(facts.qualification?.model_id).toBe('model-alpha');
  });

  it('never substitutes a document: transport failure, HTTP failure and a bad body are named', async () => {
    await expect(
      new ConsumerRouteStatusClient(async () => { throw new TypeError('offline'); }).load(),
    ).rejects.toMatchObject({ code: 'route_status_unreachable', retryable: true });

    await expect(
      new ConsumerRouteStatusClient(async () => response({}, 'application/json', 503)).load(),
    ).rejects.toMatchObject({ code: 'route_status_unavailable', retryable: true, status: 503 });

    await expect(
      new ConsumerRouteStatusClient(async () => response('<html>no</html>', 'text/html')).load(),
    ).rejects.toMatchObject({ code: 'invalid_route_status_content_type', retryable: false });

    await expect(
      new ConsumerRouteStatusClient(async () => response('{not json', 'application/json')).load(),
    ).rejects.toMatchObject({ code: 'invalid_route_status_document', retryable: false });

    const wrongProtocol = validObservatoryAdapterEvent();
    wrongProtocol.protocol = 'mycelium.observatory_stream.v2';
    await expect(
      new ConsumerRouteStatusClient(async () => response(wrongProtocol)).load(),
    ).rejects.toMatchObject({ code: 'invalid_route_status_document', retryable: false });

    await expect(
      new ConsumerRouteStatusClient(async () => response(validObservatoryAdapterEvent())).load(),
    ).resolves.toBeInstanceOf(Object);
  });

  it('summarises sessions into published state counts without inventing health', () => {
    const decoded = decodeObservatoryAdapterEvent(validObservatoryAdapterEvent());
    const facts = projectConsumerRouteStatus(decoded.bundle, decoded.generation);
    expect(facts.sessions.total).toBe(decoded.bundle.snapshot.sessions.length);
    expect(facts.sessions.by_state.completed).toBe(1);
    expect(facts.sessions.by_state.failed).toBe(0);
    expect(facts.sessions.quarantined).toEqual([]);
    expect(facts.buffered_sessions).toBe(1);
    expect(facts.quarantine_capacity).toBe(16);
    expect(facts.dropped_quarantine_count).toBe(0);
    expect(Object.isFrozen(facts)).toBe(true);
    expect(new ConsumerRouteStatusError('route_status_unreachable', true)).toMatchObject({
      name: 'ConsumerRouteStatusError',
      code: 'route_status_unreachable',
      status: null,
    });
  });
});
