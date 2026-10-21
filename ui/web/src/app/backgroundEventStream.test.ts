// SPDX-License-Identifier: AGPL-3.0-only
import { describe, expect, it } from 'vitest';
import { createPageActivity, type BackgroundEventSourceLike, type BackgroundFetch } from './backgroundFetch';

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
}

class FakeSource implements BackgroundEventSourceLike {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  readonly listeners = new Map<string, Set<(event: MessageEvent<string>) => void>>();
  constructor(readonly url: string) {}
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)?.add(listener);
  }
  removeEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.get(type)?.delete(listener);
  }
  close(): void {
    this.closed = true;
  }
  emit(type: string, data: string): void {
    for (const listener of this.listeners.get(type) ?? []) listener(new MessageEvent(type, { data }));
  }
}

function harness() {
  const win = new FakeWindow();
  const doc = new FakeDocument();
  const fetchImplementation: BackgroundFetch = () => Promise.reject(new Error('unused'));
  const activity = createPageActivity(win, doc, fetchImplementation);
  const sources: FakeSource[] = [];
  const open = (url: string) => {
    const source = new FakeSource(url);
    sources.push(source);
    return source;
  };
  return { win, activity, sources, open };
}

describe('background event streams across the page lifecycle', () => {
  it('closes the native stream when the page starts leaving, so the browser cannot reconnect it then', () => {
    const { win, activity, sources, open } = harness();
    const stream = activity.eventStream('/api/v1/observatory/events', open);
    expect(sources).toHaveLength(1);
    win.dispatchEvent(new Event('beforeunload'));
    expect(sources[0].closed).toBe(true);
    win.dispatchEvent(new Event('pagehide'));
    expect(sources).toHaveLength(1);
    stream.close();
  });

  it('opens no native stream while the page is leaving and opens it once the page is active again', () => {
    const { win, activity, sources, open } = harness();
    win.dispatchEvent(new Event('beforeunload'));
    const stream = activity.eventStream('/api/v1/observatory/events', open);
    expect(sources).toHaveLength(0);
    win.dispatchEvent(new Event('pageshow'));
    expect(sources).toHaveLength(1);
    stream.close();
  });

  it('reopens transparently after a back/forward restore with the same handlers and listeners', () => {
    const { win, activity, sources, open } = harness();
    const stream = activity.eventStream('/api/v1/observatory/events', open);
    const seen: string[] = [];
    let opens = 0;
    stream.onopen = () => {
      opens += 1;
    };
    stream.addEventListener('snapshot', (event) => seen.push(event.data));
    sources[0].onopen?.(new Event('open'));
    sources[0].emit('snapshot', 'a');
    win.dispatchEvent(new Event('pagehide'));
    win.dispatchEvent(new Event('pageshow'));
    expect(sources).toHaveLength(2);
    expect(sources[0].closed).toBe(true);
    sources[1].onopen?.(new Event('open'));
    sources[1].emit('snapshot', 'b');
    expect(opens).toBe(2);
    expect(seen).toEqual(['a', 'b']);
    stream.close();
  });

  it('a closed stream stays closed across later page activity', () => {
    const { win, activity, sources, open } = harness();
    const stream = activity.eventStream('/api/v1/observatory/events', open);
    stream.close();
    expect(sources[0].closed).toBe(true);
    win.dispatchEvent(new Event('beforeunload'));
    win.dispatchEvent(new Event('pageshow'));
    expect(sources).toHaveLength(1);
  });

  it('passes live stream errors through so disconnects still show', () => {
    const { activity, sources, open } = harness();
    const stream = activity.eventStream('/api/v1/observatory/events', open);
    let errors = 0;
    stream.onerror = () => {
      errors += 1;
    };
    sources[0].onerror?.(new Event('error'));
    expect(errors).toBe(1);
    stream.close();
  });
});
