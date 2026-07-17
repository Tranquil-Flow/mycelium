export class EvidenceParseError extends TypeError {
  readonly path: string;

  constructor(path: string, expectation: string) {
    super(`Invalid evidence at ${path}: expected ${expectation}`);
    this.name = 'EvidenceParseError';
    this.path = path;
  }
}

export type JsonRecord = Record<string, unknown>;

export function record(value: unknown, path: string): JsonRecord {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new EvidenceParseError(path, 'an object');
  }
  return value as JsonRecord;
}

export function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new EvidenceParseError(path, 'an array');
  }
  return value;
}

export function string(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new EvidenceParseError(path, 'a non-empty string');
  }
  return value;
}

/** Parse a non-empty date-time string and reject values the platform cannot represent. */
export function dateTimeString(value: unknown, path: string): string {
  const parsed = string(value, path);
  if (!Number.isFinite(Date.parse(parsed))) {
    throw new EvidenceParseError(path, 'a valid date-time string');
  }
  return parsed;
}

export function finiteNumber(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new EvidenceParseError(path, 'a finite number');
  }
  return value;
}

export function nonNegativeNumber(value: unknown, path: string): number {
  const parsed = finiteNumber(value, path);
  if (parsed < 0) {
    throw new EvidenceParseError(path, 'a non-negative finite number');
  }
  return parsed;
}

export function integer(value: unknown, path: string): number {
  const parsed = finiteNumber(value, path);
  if (!Number.isInteger(parsed)) {
    throw new EvidenceParseError(path, 'an integer');
  }
  return parsed;
}

export function nonNegativeInteger(value: unknown, path: string): number {
  const parsed = integer(value, path);
  if (parsed < 0) {
    throw new EvidenceParseError(path, 'a non-negative integer');
  }
  return parsed;
}

export function positiveInteger(value: unknown, path: string): number {
  const parsed = integer(value, path);
  if (parsed <= 0) {
    throw new EvidenceParseError(path, 'a positive integer');
  }
  return parsed;
}

export function boolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') {
    throw new EvidenceParseError(path, 'a boolean');
  }
  return value;
}

export function nullableInteger(value: unknown, path: string): number | null {
  return value === null ? null : nonNegativeInteger(value, path);
}

export function stringArray(value: unknown, path: string): string[] {
  return array(value, path).map((item, index) => string(item, `${path}[${index}]`));
}

export function oneOf<const T extends readonly string[]>(
  value: unknown,
  choices: T,
  path: string,
): T[number] {
  const parsed = string(value, path);
  if (!(choices as readonly string[]).includes(parsed)) {
    throw new EvidenceParseError(path, choices.map((choice) => JSON.stringify(choice)).join(' or '));
  }
  return parsed as T[number];
}

export function unique(values: readonly string[], path: string): void {
  if (new Set(values).size !== values.length) {
    throw new EvidenceParseError(path, 'unique values');
  }
}

export function sameStringArrays(
  actual: readonly string[],
  expected: readonly string[],
  path: string,
): void {
  if (actual.length !== expected.length || actual.some((item, index) => item !== expected[index])) {
    throw new EvidenceParseError(path, `the ordered values ${JSON.stringify(expected)}`);
  }
}

export function offlineClaimBoundary(sourceBoundary?: string): string {
  const guard = 'Offline fixture evidence only; no live network state, routing, or failover is claimed.';
  if (sourceBoundary === undefined || sourceBoundary.trim().length === 0) {
    return guard;
  }
  return `${guard} Source boundary: ${sourceBoundary}`;
}

/**
 * Recursively freezes an object graph in place.
 *
 * Adapter outputs contain only data objects and arrays, but this deliberately
 * handles every own key (including symbols) and circular references so callers
 * can safely use it as the single runtime immutability boundary.
 */
export function deepFreeze<T>(value: T): T {
  const seen = new WeakSet<object>();

  function freeze(current: unknown): void {
    if ((typeof current !== 'object' || current === null) && typeof current !== 'function') {
      return;
    }

    const object = current as object;
    if (seen.has(object)) {
      return;
    }
    seen.add(object);

    for (const key of Reflect.ownKeys(object)) {
      freeze((object as Record<PropertyKey, unknown>)[key]);
    }
    Object.freeze(object);
  }

  freeze(value);
  return value;
}
