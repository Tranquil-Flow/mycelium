import { describe, expect, it, vi } from 'vitest';
import { consumeDeviceLabOperatorCapability } from './deviceLabCapability';

const token = 'operator_capability_abcdefghijklmnopqrstuvwxyz0123456789';

describe('Device Lab operator capability fragment', () => {
  it('accepts the Device Lab form, returns it only in memory, and scrubs browser history', () => {
    const replaceState = vi.fn();
    const value = consumeDeviceLabOperatorCapability(
      { hash: `#lab/operator/${token}`, pathname: '/console', search: '?local=1' },
      { replaceState },
    );

    expect(value).toBe(token);
    expect(replaceState).toHaveBeenCalledWith(null, '', '/console?local=1#lab');
  });

  it('supports the legacy operator form without retaining it', () => {
    const replaceState = vi.fn();
    expect(consumeDeviceLabOperatorCapability(
      { hash: `#operator/${token}`, pathname: '/', search: '' },
      { replaceState },
    )).toBe(token);
    expect(replaceState).toHaveBeenCalledWith(null, '', '/#lab');
  });

  it('scrubs malformed capability-shaped fragments and rejects unrelated fragments', () => {
    const malformedHistory = { replaceState: vi.fn() };
    expect(consumeDeviceLabOperatorCapability(
      { hash: '#lab/operator/too-short', pathname: '/', search: '' },
      malformedHistory,
    )).toBeNull();
    expect(malformedHistory.replaceState).toHaveBeenCalledWith(null, '', '/#lab');

    const unrelatedHistory = { replaceState: vi.fn() };
    expect(consumeDeviceLabOperatorCapability(
      { hash: '#network', pathname: '/', search: '' },
      unrelatedHistory,
    )).toBeNull();
    expect(unrelatedHistory.replaceState).not.toHaveBeenCalled();
  });
});
