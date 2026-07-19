import { afterEach, describe, expect, it } from 'vitest';
import {
  ProductNetworkPolicyError,
  installProductNetworkRecorder,
  type ProductNetworkRecorder,
} from './networkRecorder';

let recorder: ProductNetworkRecorder | null = null;

afterEach(() => {
  recorder?.restore();
  recorder = null;
});

describe('product network recorder', () => {
  it('records same-origin capability calls without retaining bodies', async () => {
    recorder = installProductNetworkRecorder('http://localhost');
    await fetch('/api/v1/inference', {
      method: 'POST',
      headers: { 'X-Mycelium-CSRF': 'test-capability' },
      body: JSON.stringify({ private: 'not retained' }),
    });
    expect(recorder.requests).toEqual([
      {
        transport: 'fetch',
        method: 'POST',
        url: 'http://localhost/api/v1/inference',
        header_names: ['x-mycelium-csrf'],
        body_present: true,
      },
    ]);
    expect(JSON.stringify(recorder.requests)).not.toContain('not retained');
  });

  it('blocks every cross-origin transport before dispatch', async () => {
    recorder = installProductNetworkRecorder('http://localhost');
    await expect(fetch('https://surveillance.invalid/collect')).rejects.toEqual(
      new ProductNetworkPolicyError('cross_origin_request_blocked'),
    );
    expect(() => new EventSource('https://surveillance.invalid/events')).toThrow(
      'cross_origin_request_blocked',
    );
    expect(() => new WebSocket('wss://surveillance.invalid/socket')).toThrow(
      'cross_origin_request_blocked',
    );
    expect(() => navigator.sendBeacon('https://surveillance.invalid/ping')).toThrow(
      'cross_origin_request_blocked',
    );
    expect(recorder.requests).toHaveLength(0);
  });

  it('blocks bearer-style authorization even on same origin', async () => {
    recorder = installProductNetworkRecorder('http://localhost');
    await expect(
      fetch('/api/v1/inference', { headers: { Authorization: 'Bearer forbidden' } }),
    ).rejects.toEqual(new ProductNetworkPolicyError('upstream_authorization_forbidden'));
    expect(recorder.requests).toHaveLength(0);
  });

  it('scrubs query and fragment data while detecting Request bodies', async () => {
    recorder = installProductNetworkRecorder('http://localhost');
    const request = new Request('http://localhost/api/v1/inference?prompt=private#fragment', {
      method: 'POST',
      body: 'private body',
    });
    await fetch(request);
    expect(recorder.requests[0]).toMatchObject({
      url: 'http://localhost/api/v1/inference',
      body_present: true,
    });
    expect(JSON.stringify(recorder.requests)).not.toContain('private');
  });

  it('rejects URL, XHR, and EventSource credential channels', async () => {
    recorder = installProductNetworkRecorder('http://localhost');
    await expect(fetch('http://user:password@localhost/api/v1/inference')).rejects.toThrow(
      'cross_origin_request_blocked',
    );
    const xhr = new XMLHttpRequest();
    expect(() =>
      xhr.open('GET', '/api/v1/swarm/status', true, 'user', 'password'),
    ).toThrow('upstream_authorization_forbidden');
    expect(
      () => new EventSource('/api/v1/observatory/events', { withCredentials: true }),
    ).toThrow('upstream_authorization_forbidden');
    expect(recorder.requests).toHaveLength(0);
  });

  it('records EventSource, WebSocket, beacon, and XHR attempts', () => {
    recorder = installProductNetworkRecorder('http://localhost');
    const events = new EventSource('/api/v1/observatory/events');
    const socket = new WebSocket('ws://localhost/api/v1/swarm/socket');
    socket.send('not retained');
    navigator.sendBeacon('/api/v1/telemetry/ping');
    const xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/v1/swarm/status');
    xhr.send();
    events.close();
    socket.close();
    expect(recorder.requests.map((request) => request.transport)).toEqual([
      'eventsource',
      'websocket',
      'beacon',
      'xhr',
    ]);
    expect(recorder.requests[1]?.body_present).toBe(true);
    expect(JSON.stringify(recorder.requests)).not.toContain('not retained');
  });
});
