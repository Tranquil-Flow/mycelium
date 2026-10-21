// SPDX-License-Identifier: AGPL-3.0-only
import { afterEach, describe, expect, it, vi } from 'vitest';
import { NAVIGATION_PENDING_RESUME_MS, createPageActivity, type BackgroundFetch } from './backgroundFetch';

class FakeWindow extends EventTarget {
  setTimeout(handler: () => void, timeout: number): number {
    return window.setTimeout(handler, timeout);
  }
  clearTimeout(id: number): void {
    window.clearTimeout(id);
  }
}

class FakeDocument extends EventTarget {
  visibilityState: DocumentVisibilityState = 'visible';
  setVisibility(state: DocumentVisibilityState): void {
    this.visibilityState = state;
    this.dispatchEvent(new Event('visibilitychange'));
  }
}

function harness() {
  const win = new FakeWindow();
  const doc = new FakeDocument();
  const started: Array<{ url: string; init: RequestInit | undefined }> = [];
  const pending: Array<{ resolve: (response: Response) => void; reject: (error: unknown) => void }> = [];
  const fetchImplementation: BackgroundFetch = vi.fn((input, init) => {
    started.push({ url: String(input), init });
    return new Promise<Response>((resolve, reject) => {
      pending.push({ resolve, reject });
      init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
    });
  });
  const activity = createPageActivity(win, doc, fetchImplementation);
  return { win, doc, started, pending, activity };
}

const settled = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('background reads across the page lifecycle', () => {
  afterEach(() => vi.useRealTimers());

  it('is plain fetch while the page is live and keeps live failures visible', async () => {
    const { activity, started, pending } = harness();
    const read = activity.fetch('/__mycelium/live-status', { method: 'GET' });
    expect(started.map((item) => item.url)).toEqual(['/__mycelium/live-status']);
    pending[0].reject(new TypeError('Load failed'));
    await expect(read).rejects.toThrow('Load failed');
    const ok = activity.fetch('/__mycelium/live-status');
    pending[1].resolve(new Response('{}', { status: 503 }));
    expect((await ok).status).toBe(503);
  });

  it('starts no read after beforeunload and resumes when the navigation does not happen', async () => {
    vi.useFakeTimers();
    const { win, activity, started, pending } = harness();
    win.dispatchEvent(new Event('beforeunload'));
    expect(activity.active()).toBe(false);
    const first = activity.fetch('/__mycelium/live-status');
    const second = activity.fetch('/__mycelium/live-status');
    const other = activity.fetch('/__mycelium/models/operation');
    expect(started).toEqual([]);
    vi.advanceTimersByTime(NAVIGATION_PENDING_RESUME_MS);
    expect(activity.active()).toBe(true);
    // Identical waiting reads share one request; every caller gets a body.
    expect(started.map((item) => item.url)).toEqual(['/__mycelium/live-status', '/__mycelium/models/operation']);
    pending[0].resolve(new Response('{"a":1}'));
    pending[1].resolve(new Response('{"b":2}'));
    await expect((await first).json()).resolves.toEqual({ a: 1 });
    await expect((await second).json()).resolves.toEqual({ a: 1 });
    await expect((await other).json()).resolves.toEqual({ b: 2 });
  });

  it('aborts reads in flight at pagehide and resumes after a back-forward cache restore', async () => {
    const { win, activity, started } = harness();
    const inFlight = activity.fetch('/api/v1/observatory/events', { headers: { Accept: 'text/event-stream' } });
    win.dispatchEvent(new Event('pagehide'));
    await expect(inFlight).rejects.toMatchObject({ name: 'AbortError' });
    const waiting = activity.fetch('/__mycelium/live-status');
    await settled();
    expect(started).toHaveLength(1);
    win.dispatchEvent(new Event('pageshow'));
    expect(started.map((item) => item.url)).toEqual(['/api/v1/observatory/events', '/__mycelium/live-status']);
    expect(waiting).toBeInstanceOf(Promise);
  });

  it('defers reads while hidden and starts them once visible', () => {
    const { doc, activity, started } = harness();
    doc.setVisibility('hidden');
    void activity.fetch('/__mycelium/artifacts/acquisitions');
    void activity.fetch('/__mycelium/artifacts/acquisitions');
    expect(started).toEqual([]);
    doc.setVisibility('visible');
    expect(started.map((item) => item.url)).toEqual(['/__mycelium/artifacts/acquisitions']);
  });

  it('lets a waiting caller abort without starting its read', async () => {
    const { doc, activity, started } = harness();
    doc.setVisibility('hidden');
    const controller = new AbortController();
    const read = activity.fetch('/__mycelium/runtime/admission-status', { signal: controller.signal });
    controller.abort();
    await expect(read).rejects.toMatchObject({ name: 'AbortError' });
    doc.setVisibility('visible');
    expect(started).toEqual([]);
  });

  it('never delays user mutations', () => {
    const { win, activity, started } = harness();
    win.dispatchEvent(new Event('beforeunload'));
    void activity.fetch('/__mycelium/deployments/select', { method: 'POST', body: '{}' });
    expect(started.map((item) => [item.url, item.init?.method])).toEqual([['/__mycelium/deployments/select', 'POST']]);
  });
});
