import { describe, expect, it } from 'vitest';
import {
  renderFreshness,
  renderLossRatio,
  renderMetric,
  renderPathClass,
  renderPseudonym,
  renderRelayRegion,
  validateRelayReference,
} from './projections';

const PSEUDONYM = 'sha256:' + 'a'.repeat(64);
const RELAY_REFERENCE = 'hmac-sha256:' + 'b'.repeat(64);

describe('renderMetric (unknown-not-zero invariant)', () => {
  it('renders null as "unknown" never "0"', () => {
    expect(renderMetric(null)).toBe('unknown');
    expect(renderMetric(null)).not.toBe('0');
  });

  it('renders numeric values as their string form', () => {
    expect(renderMetric(0)).toBe('0');
    expect(renderMetric(12)).toBe('12');
    expect(renderMetric(1_500_000)).toBe('1500000');
  });
});

describe('renderPathClass', () => {
  it('renders direct, relay, and unknown as themselves', () => {
    expect(renderPathClass('direct')).toBe('direct');
    expect(renderPathClass('relay')).toBe('relay');
    expect(renderPathClass('unknown')).toBe('unknown');
  });
});

describe('renderFreshness', () => {
  it('renders current, stale, unknown', () => {
    expect(renderFreshness('current')).toBe('current');
    expect(renderFreshness('stale')).toBe('stale');
    expect(renderFreshness('unknown')).toBe('unknown');
  });
});

describe('renderRelayRegion', () => {
  it('renders coarse region strings and unknown', () => {
    expect(renderRelayRegion('europe-west')).toBe('europe-west');
    expect(renderRelayRegion('us-east')).toBe('us-east');
    expect(renderRelayRegion('unknown')).toBe('unknown');
  });
});

describe('validateRelayReference (privacy-safe relay projection)', () => {
  it('accepts hmac-sha256:64hex references', () => {
    expect(validateRelayReference(RELAY_REFERENCE)).toBe(true);
  });

  it('rejects raw relay URLs', () => {
    expect(validateRelayReference('wss://relay.example.test/abc')).toBe(false);
    expect(validateRelayReference('https://relay.example.test')).toBe(false);
    expect(validateRelayReference('relay://host/path')).toBe(false);
  });

  it('rejects sha256-only references and bare hex', () => {
    expect(validateRelayReference(PSEUDONYM)).toBe(false);
    expect(validateRelayReference('b'.repeat(64))).toBe(false);
  });

  it('rejects malformed hmac-sha256 references', () => {
    expect(validateRelayReference('hmac-sha256:abc')).toBe(false);
    expect(validateRelayReference('hmac-sha256:' + 'z'.repeat(64))).toBe(false);
    expect(validateRelayReference('hmac-sha256:' + 'b'.repeat(63) + 'Z')).toBe(false);
    expect(validateRelayReference('')).toBe(false);
  });
});

describe('renderLossRatio (explicit-zero rule)', () => {
  it('returns "unknown" for null', () => {
    expect(renderLossRatio(null, null, null)).toBe('unknown');
    expect(renderLossRatio(null, 5, false)).toBe('unknown');
  });

  it('accepts explicit zero only when measuredZero is true and sampleCount >= 1', () => {
    expect(renderLossRatio(0, 1, true)).toBe('0');
    expect(renderLossRatio(0, 64, true)).toBe('0');
  });

  it('rejects zero without measuredZero or without sampleCount', () => {
    expect(() => renderLossRatio(0, null, null)).toThrow();
    expect(() => renderLossRatio(0, null, true)).toThrow();
    expect(() => renderLossRatio(0, 0, true)).toThrow();
    expect(() => renderLossRatio(0, 5, false)).toThrow();
  });

  it('renders a positive measured ratio as a percentage string', () => {
    expect(renderLossRatio(0.5, 10, false)).toMatch(/50/);
    expect(renderLossRatio(0.01, 200, false)).toMatch(/1/);
  });
});

describe('renderPseudonym (only sha256:64hex passes)', () => {
  it('returns the pseudonym when valid', () => {
    expect(renderPseudonym(PSEUDONYM)).toBe(PSEUDONYM);
  });

  it('returns "unknown" for null or malformed input', () => {
    expect(renderPseudonym(null)).toBe('unknown');
    expect(renderPseudonym('sha256:short')).toBe('unknown');
    expect(renderPseudonym('sha256:' + 'Z'.repeat(64))).toBe('unknown');
    expect(renderPseudonym('')).toBe('unknown');
  });

  it('never renders a raw endpoint id or relay reference', () => {
    const rawEndpointId = 'endpoint-12345abcdef';
    expect(renderPseudonym(rawEndpointId)).toBe('unknown');
    expect(renderPseudonym(RELAY_REFERENCE)).toBe('unknown');
  });
});