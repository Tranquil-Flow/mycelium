// SPDX-License-Identifier: AGPL-3.0-only
import { parseProductRoute } from './navigation';

export type ApplicationSurface = 'consumer' | 'operator' | 'not-found';

/** Consumer anchors are not operator workspace selectors. Legacy workspace URLs
 * move to /operator, preserving their hash and same-origin capability semantics. */
export function applicationSurface(pathname: string, hash: string): ApplicationSurface {
  if (pathname === '/operator' || pathname === '/operator/') return 'operator';
  if (pathname !== '/') return 'not-found';
  return parseProductRoute(hash) === null ? 'consumer' : 'operator';
}

/** Call only after consumeDeviceLabOperatorCapability has scrubbed the fragment. */
export function canonicalizeApplicationPath(): ApplicationSurface {
  const surface = applicationSurface(window.location.pathname, window.location.hash);
  if (surface === 'operator' && window.location.pathname === '/') {
    window.history.replaceState(null, '', `/operator${window.location.search}${window.location.hash}`);
  }
  return surface;
}
