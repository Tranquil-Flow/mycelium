const PRIVATE_IPV4_RANGES: readonly [number, number][] = [
  [0x0a000000, 0x0affffff],
  [0x7f000000, 0x7fffffff],
  [0xa9fe0000, 0xa9feffff],
  [0xac100000, 0xac1fffff],
  [0xc0a80000, 0xc0a8ffff],
  [0x64400000, 0x647fffff],
];

function ipv4Number(value: string): number | null {
  const octets = value.split('.');
  if (octets.length !== 4) return null;
  const numbers = octets.map(Number);
  if (numbers.some((part, index) => !/^\d{1,3}$/.test(octets[index]) || part < 0 || part > 255)) {
    return null;
  }
  return numbers.reduce((sum, part) => sum * 256 + part, 0);
}

function candidateHost(value: string): string {
  const trimmed = value.trim();
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)) {
    try {
      return new URL(trimmed).hostname.replace(/^\[|\]$/g, '').toLowerCase();
    } catch {
      return trimmed.toLowerCase();
    }
  }
  const bracket = trimmed.match(/^\[([^\]]+)\](?::\d+)?$/);
  if (bracket) return bracket[1].toLowerCase();
  const hostPort = trimmed.match(/^([^:]+):\d+$/);
  return (hostPort?.[1] ?? trimmed).toLowerCase();
}

export function isPrivateAddress(value: string): boolean {
  const host = candidateHost(value);
  if (host === 'localhost' || host.endsWith('.localhost')) return true;
  const ipv4 = ipv4Number(host);
  if (ipv4 !== null) {
    return PRIVATE_IPV4_RANGES.some(([start, end]) => ipv4 >= start && ipv4 <= end);
  }
  const normalized = host.toLowerCase();
  return normalized === '::1' ||
    normalized.startsWith('fc') ||
    normalized.startsWith('fd') ||
    /^fe[89ab][0-9a-f]:/.test(normalized);
}

export function displayEndpointIdentity(endpointId: string | null): string {
  if (endpointId === null || endpointId.length === 0) return 'Endpoint not disclosed';
  return isPrivateAddress(endpointId) ? 'Private address redacted' : endpointId;
}

export function redactPrivateAddresses<T>(value: T): T {
  const seen = new WeakSet<object>();
  const visit = (candidate: unknown): unknown => {
    if (typeof candidate === 'string') {
      return isPrivateAddress(candidate) ? '[redacted private address]' : candidate;
    }
    if (candidate === null || typeof candidate !== 'object') return candidate;
    if (seen.has(candidate)) return '[redacted circular value]';
    seen.add(candidate);
    if (Array.isArray(candidate)) return candidate.map(visit);
    const output: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(candidate)) output[key] = visit(item);
    return output;
  };
  return visit(value) as T;
}
