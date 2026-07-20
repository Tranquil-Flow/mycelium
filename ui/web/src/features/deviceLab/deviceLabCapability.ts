const CAPABILITY_FRAGMENT = /^#(?:lab\/operator|operator)\/([A-Za-z0-9_-]{32,512})$/;
const CAPABILITY_PREFIX = /^#(?:lab\/operator|operator)\//;

export interface DeviceLabLocation {
  readonly hash: string;
  readonly pathname: string;
  readonly search: string;
}

export interface DeviceLabHistory {
  replaceState(data: unknown, unused: string, url?: string | URL | null): void;
}

/**
 * Consume an operator capability exactly once from the URL fragment.
 *
 * Fragments are not sent to the server. The capability is returned to the caller for
 * in-memory use and immediately removed from browser history. Malformed capability-shaped
 * fragments are also scrubbed, but never accepted.
 */
export function consumeDeviceLabOperatorCapability(
  location: DeviceLabLocation = window.location,
  history: DeviceLabHistory = window.history,
): string | null {
  const match = CAPABILITY_FRAGMENT.exec(location.hash);
  if (match !== null) {
    history.replaceState(null, '', `${location.pathname}${location.search}#lab`);
    return match[1];
  }
  if (CAPABILITY_PREFIX.test(location.hash)) {
    history.replaceState(null, '', `${location.pathname}${location.search}#lab`);
  }
  return null;
}
