import { describe, expect, it } from 'vitest';
import { displayEndpointIdentity, isPrivateAddress, redactPrivateAddresses } from './privacy';

describe('swarm privacy projection', () => {
  it.each([
    '10.0.0.7',
    '127.0.0.1:8787',
    '172.16.4.2',
    '192.168.1.20',
    '169.254.4.8',
    '100.64.0.9',
    '::1',
    'fd12:3456::9',
    'fe80::2',
    'http://localhost:8787',
    'https://192.168.1.2/device',
  ])('recognizes private address material: %s', (value) => {
    expect(isPrivateAddress(value)).toBe(true);
    expect(displayEndpointIdentity(value)).toBe('Private address redacted');
  });

  it('keeps opaque endpoint identities while redacting private addresses recursively', () => {
    expect(displayEndpointIdentity('endpoint-id-abc123')).toBe('endpoint-id-abc123');
    expect(
      redactPrivateAddresses({
        endpoint_id: 'endpoint-id-abc123',
        private_address: '192.168.1.4',
        candidates: ['https://relay.example.test', '10.0.0.5'],
      }),
    ).toEqual({
      endpoint_id: 'endpoint-id-abc123',
      private_address: '[redacted private address]',
      candidates: ['https://relay.example.test', '[redacted private address]'],
    });
  });
});
