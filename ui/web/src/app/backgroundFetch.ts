// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Page-lifecycle gate for background reads (polls and resumable streams).
 *
 * A page keeps running while a reload, Back/Forward or link navigation is
 * pending: its timers still fire until the next document commits. A fetch the
 * old document starts in that window is refused by the engine; WebKit logs it
 * as a JavaScript error ("Fetch API cannot load <url> due to access control
 * checks."), which automation reports as a page error, and the request was
 * never useful anyway. While the page is hidden, background reads are wasted
 * work.
 *
 * `backgroundFetch` is a drop-in `fetch` for background GET reads:
 *   - while the page is visible and not navigating away it is plain `fetch`;
 *   - while hidden, or after `beforeunload`/`pagehide`, it does not start the
 *     request; the call waits and starts once the page is active again
 *     (visible, `pageshow`, or the navigation turned out not to happen).
 *     Identical waiting reads share one request (each caller gets a clone),
 *     so a timer that kept ticking while hidden does not burst on return;
 *   - requests in flight when the page is hidden for navigation (`pagehide`)
 *     are aborted (AbortError), so nothing is left for the old document.
 * A read that fails while the page is live is returned exactly as `fetch`
 * returns it, so the product's degraded/offline states still show real
 * failures. Non-GET requests (user mutations) always pass straight through.
 *
 * `backgroundEventStream` does the same for server-sent event streams: the
 * browser reconnects a native EventSource by itself, and a reconnect the old
 * document starts while a navigation is pending is refused (and logged) the
 * same way. The wrapper closes the native stream when the page starts leaving
 * (`beforeunload`/`pagehide`), opens none while it is leaving, and reopens it
 * with the same handlers and listeners once the page is active again (the
 * navigation did not happen, or a back/forward restore). Live stream errors
 * pass through unchanged.
 */

export type BackgroundFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/** After `beforeunload` without an unload (a cancelled or download navigation). */
export const NAVIGATION_PENDING_RESUME_MS = 10_000;

interface LifecycleTarget {
  addEventListener(type: string, listener: (event: Event) => void): void;
  setTimeout(handler: () => void, timeout: number): number;
  clearTimeout(id: number): void;
}

interface VisibilityTarget {
  readonly visibilityState: DocumentVisibilityState | string;
  addEventListener(type: string, listener: () => void): void;
}

interface Waiter {
  readonly resolve: (response: Response) => void;
  readonly reject: (reason: unknown) => void;
  readonly signal: AbortSignal | null;
  readonly onAbort: () => void;
}

interface DeferredRead {
  readonly input: RequestInfo | URL;
  readonly init: RequestInit | undefined;
  readonly waiters: Set<Waiter>;
}

function abortError(): DOMException {
  return new DOMException('The background read was aborted.', 'AbortError');
}

function methodOf(input: RequestInfo | URL, init: RequestInit | undefined): string {
  const method = init?.method ?? (typeof Request !== 'undefined' && input instanceof Request ? input.method : 'GET');
  return method.toUpperCase();
}

function readKey(input: RequestInfo | URL, init: RequestInit | undefined): string {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  const headers = [...new Headers(init?.headers).entries()].sort(([a], [b]) => a.localeCompare(b));
  return JSON.stringify([url, init?.cache ?? null, init?.credentials ?? null, init?.redirect ?? null, headers]);
}

/** The part of a native EventSource the wrapper drives. */
export interface BackgroundEventSourceLike {
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  removeEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  close(): void;
}

export type OpenEventSource = (url: string) => BackgroundEventSourceLike;

export interface PageActivity {
  /** True when a background read may start now. */
  readonly active: () => boolean;
  readonly fetch: BackgroundFetch;
  /** A lifecycle-aware event stream; see the module comment. */
  readonly eventStream: (url: string, open: OpenEventSource) => BackgroundEventSourceLike;
}

interface LifecycleListener {
  readonly leave: () => void;
  readonly resume: () => void;
}

export function createPageActivity(
  win: LifecycleTarget,
  doc: VisibilityTarget,
  fetchImplementation: BackgroundFetch,
): PageActivity {
  let leaving = false;
  let resumeTimer: number | null = null;
  const deferred = new Map<string, DeferredRead>();
  const inFlight = new Set<AbortController>();
  const lifecycle = new Set<LifecycleListener>();
  const notifyLeave = () => {
    for (const listener of [...lifecycle]) listener.leave();
  };

  const active = () => !leaving && doc.visibilityState !== 'hidden';

  const start = (input: RequestInfo | URL, init: RequestInit | undefined): Promise<Response> => {
    const controller = new AbortController();
    const external = init?.signal ?? null;
    const forward = () => controller.abort(external?.reason);
    if (external?.aborted === true) return Promise.reject(external.reason ?? abortError());
    external?.addEventListener('abort', forward, { once: true });
    inFlight.add(controller);
    let request: Promise<Response>;
    try {
      request = fetchImplementation(input, { ...init, signal: controller.signal });
    } catch (error) {
      request = Promise.reject(error);
    }
    // Only the request phase is tracked: once headers arrive the caller owns
    // the body, and a navigation that later replaces the document closes it.
    // The caller's own signal keeps controlling the body after that.
    return request.finally(() => inFlight.delete(controller));
  };

  const flush = () => {
    if (!active()) return;
    const reads = [...deferred.values()];
    deferred.clear();
    for (const read of reads) {
      const waiters = [...read.waiters].filter((waiter) => waiter.signal?.aborted !== true);
      for (const waiter of read.waiters) waiter.signal?.removeEventListener('abort', waiter.onAbort);
      if (waiters.length === 0) continue;
      const shared = waiters.length === 1 ? waiters[0].signal ?? undefined : undefined;
      void start(read.input, { ...read.init, signal: shared }).then((response) => {
        waiters.forEach((waiter, index) => {
          if (waiter.signal?.aborted === true) waiter.reject(waiter.signal.reason ?? abortError());
          else waiter.resolve(index === waiters.length - 1 ? response : response.clone());
        });
      }, (error: unknown) => {
        for (const waiter of waiters) waiter.reject(error);
      });
    }
  };

  const resume = () => {
    if (resumeTimer !== null) win.clearTimeout(resumeTimer);
    resumeTimer = null;
    leaving = false;
    for (const listener of [...lifecycle]) listener.resume();
    flush();
  };

  win.addEventListener('beforeunload', () => {
    leaving = true;
    notifyLeave();
    if (resumeTimer !== null) win.clearTimeout(resumeTimer);
    resumeTimer = win.setTimeout(resume, NAVIGATION_PENDING_RESUME_MS);
  });
  win.addEventListener('pagehide', () => {
    leaving = true;
    notifyLeave();
    if (resumeTimer !== null) win.clearTimeout(resumeTimer);
    resumeTimer = null;
    for (const controller of inFlight) controller.abort(abortError());
    inFlight.clear();
  });
  win.addEventListener('pageshow', resume);
  doc.addEventListener('visibilitychange', flush);

  const backgroundFetch: BackgroundFetch = (input, init) => {
    if (methodOf(input, init) !== 'GET') return fetchImplementation(input, init);
    if (active()) return start(input, init);
    const signal = init?.signal ?? null;
    if (signal?.aborted === true) return Promise.reject(signal.reason ?? abortError());
    const key = readKey(input, init);
    let read = deferred.get(key);
    if (read === undefined) {
      read = { input, init: { ...init, signal: undefined }, waiters: new Set() };
      deferred.set(key, read);
    }
    const waiting = read;
    return new Promise<Response>((resolve, reject) => {
      const waiter: Waiter = {
        resolve,
        reject,
        signal,
        onAbort: () => {
          waiting.waiters.delete(waiter);
          if (waiting.waiters.size === 0 && deferred.get(key) === waiting) deferred.delete(key);
          reject(signal?.reason ?? abortError());
        },
      };
      signal?.addEventListener('abort', waiter.onAbort, { once: true });
      waiting.waiters.add(waiter);
    });
  };

  const eventStream = (url: string, open: OpenEventSource): BackgroundEventSourceLike => {
    const listeners = new Map<string, Set<(event: MessageEvent<string>) => void>>();
    let source: BackgroundEventSourceLike | null = null;
    let closed = false;
    const stream: BackgroundEventSourceLike = {
      onopen: null,
      onerror: null,
      addEventListener(type, listener) {
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type)?.add(listener);
        source?.addEventListener(type, listener);
      },
      removeEventListener(type, listener) {
        listeners.get(type)?.delete(listener);
        source?.removeEventListener(type, listener);
      },
      close() {
        closed = true;
        detach();
        lifecycle.delete(watcher);
      },
    };
    const attach = () => {
      if (closed || source !== null || leaving) return;
      const next = open(url);
      next.onopen = (event) => stream.onopen?.(event);
      next.onerror = (event) => stream.onerror?.(event);
      for (const [type, set] of listeners) for (const listener of set) next.addEventListener(type, listener);
      source = next;
    };
    const detach = () => {
      if (source === null) return;
      const current = source;
      source = null;
      current.onopen = null;
      current.onerror = null;
      for (const [type, set] of listeners) for (const listener of set) current.removeEventListener(type, listener);
      current.close();
    };
    const watcher: LifecycleListener = { leave: detach, resume: attach };
    lifecycle.add(watcher);
    attach();
    return stream;
  };

  return Object.freeze({ active, fetch: backgroundFetch, eventStream });
}

let browserActivity: PageActivity | null = null;

function browserPageActivity(): PageActivity | null {
  if (browserActivity !== null) return browserActivity;
  if (typeof window === 'undefined' || typeof document === 'undefined') return null;
  browserActivity = createPageActivity(
    window,
    document,
    (input, init) => globalThis.fetch(input, init),
  );
  return browserActivity;
}

/** Server-sent event stream for background updates; see the module comment. */
export function backgroundEventStream(url: string): BackgroundEventSourceLike {
  const open: OpenEventSource = (target) => new EventSource(target) as unknown as BackgroundEventSourceLike;
  const activity = browserPageActivity();
  return activity === null ? open(url) : activity.eventStream(url, open);
}

/** `fetch` for background reads; see the module comment. */
export const backgroundFetch: BackgroundFetch = (input, init) => {
  const activity = browserPageActivity();
  return activity === null ? globalThis.fetch(input, init) : activity.fetch(input, init);
};
