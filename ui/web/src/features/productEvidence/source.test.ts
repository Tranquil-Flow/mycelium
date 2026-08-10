import { describe, expect, it } from 'vitest';
import fixture from '../../../../../contracts/compatibility-fixtures/product-snapshot-v1.json';
import { LiveProductEvidenceSource } from './source';

function response(document: unknown, contentType = 'application/json', status = 200) {
  const bytes = new TextEncoder().encode(
    typeof document === 'string' ? document : JSON.stringify(document),
  );
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) => {
        if (name.toLowerCase() === 'content-length') return String(bytes.length);
        if (name.toLowerCase() === 'content-type') return contentType;
        return null;
      },
    },
    body: new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(bytes); controller.close(); },
    }),
  };
}

function nextEvent(cursor: number) {
  const snapshot = structuredClone(fixture);
  snapshot.publication.cursor = cursor;
  snapshot.publication.generation = cursor;
  snapshot.publication.snapshot_id = `snapshot-${cursor}`;
  return {
    protocol: 'mycelium.product_event.v1',
    cursor,
    previous_cursor: cursor - 1,
    event_kind: 'snapshot_published',
    snapshot,
  };
}

function frame(cursor: number): string {
  return `id: ${cursor}\nevent: product_snapshot\ndata: ${JSON.stringify(nextEvent(cursor))}\n\n`;
}

describe('live product evidence source', () => {
  it('uses Last-Event-ID and advances only contiguous bounded frames', async () => {
    const scheduled: Array<() => void> = [];
    const requests: Array<{ url: string; headers: Readonly<Record<string, string>> }> = [];
    const source = new LiveProductEvidenceSource({
      fetcher: async (url, init) => {
        requests.push({ url, headers: init.headers });
        return url.endsWith('/snapshot')
          ? response(fixture)
          : response(frame(2), 'text/event-stream; charset=utf-8');
      },
      now: () => fixture.publication.published_at_unix_ms,
      schedule: (callback) => { scheduled.push(callback); return scheduled.length; },
      cancelSchedule: () => undefined,
    });
    source.subscribe(() => undefined);
    await source.loadInitial();
    await Promise.resolve();
    scheduled.shift()?.();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(source.getState()?.cursor).toBe(2);
    expect(source.getState()?.status).toBe('connected');
    expect(requests[1].headers['Last-Event-ID']).toBe('1');
  });

  it('preserves the last snapshot and disconnects on a cursor gap', async () => {
    const scheduled: Array<() => void> = [];
    const source = new LiveProductEvidenceSource({
      fetcher: async (url) => url.endsWith('/snapshot')
        ? response(fixture)
        : response(frame(3), 'text/event-stream'),
      now: () => fixture.publication.published_at_unix_ms,
      schedule: (callback) => { scheduled.push(callback); return scheduled.length; },
      cancelSchedule: () => undefined,
    });
    source.subscribe(() => undefined);
    await source.loadInitial();
    await Promise.resolve();
    scheduled.shift()?.();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(source.getState()?.cursor).toBe(1);
    expect(source.getState()?.status).toBe('disconnected');
    expect(source.getState()?.reason_code).toBe('product_cursor_gap');
  });

  it('recovers from an expired replay cursor using an authoritative snapshot', async () => {
    const scheduled: Array<() => void> = [];
    const replacement = structuredClone(fixture);
    replacement.publication.cursor = 7;
    replacement.publication.generation = 7;
    replacement.publication.snapshot_id = 'snapshot-7';
    let snapshotRequests = 0;
    const source = new LiveProductEvidenceSource({
      fetcher: async (url) => {
        if (url.endsWith('/snapshot')) {
          snapshotRequests += 1;
          return response(snapshotRequests === 1 ? fixture : replacement);
        }
        return response('', 'text/event-stream', 409);
      },
      now: () => fixture.publication.published_at_unix_ms,
      schedule: (callback) => { scheduled.push(callback); return scheduled.length; },
      cancelSchedule: () => undefined,
    });
    source.subscribe(() => undefined);
    await source.loadInitial();
    await Promise.resolve();
    scheduled.shift()?.();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(snapshotRequests).toBe(2);
    expect(source.getState()?.cursor).toBe(7);
    expect(source.getState()?.status).toBe('connected');
    expect(source.getState()?.reason_code).toBeNull();
  });
});
