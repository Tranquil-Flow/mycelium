// SPDX-License-Identifier: AGPL-3.0-only
import { describe, expect, it, vi } from 'vitest';
import { errorText, scanWithPermission } from './api';

function fakeScanner(check: string, request: string) {
  return {
    checkPermissions: vi.fn(async () => check),
    requestPermissions: vi.fn(async () => request),
    scan: vi.fn(async () => ({ content: 'mycelium:QR', format: 'QR_CODE', bounds: null })),
    Format: { QRCode: 'QR_CODE' },
  } as unknown as Parameters<typeof scanWithPermission>[0];
}

describe('scanWithPermission', () => {
  it('scans straight away when the camera is already granted', async () => {
    const scanner = fakeScanner('granted', 'granted');
    expect(await scanWithPermission(scanner)).toBe('mycelium:QR');
    expect(scanner.requestPermissions).not.toHaveBeenCalled();
  });

  it('asks for the camera before scanning', async () => {
    const scanner = fakeScanner('prompt', 'granted');
    expect(await scanWithPermission(scanner)).toBe('mycelium:QR');
    expect(scanner.requestPermissions).toHaveBeenCalledTimes(1);
    expect(scanner.scan).toHaveBeenCalledWith({ formats: ['QR_CODE'], windowed: false });
  });

  it('refuses with a readable reason and never opens the camera when denied', async () => {
    const scanner = fakeScanner('prompt', 'denied');
    await expect(scanWithPermission(scanner)).rejects.toMatch(/^camera_permission_denied/);
    expect(scanner.scan).not.toHaveBeenCalled();
  });
});

describe('errorText', () => {
  it('never renders an object as [object Object]', () => {
    expect(errorText('seed_join_refused:x')).toBe('seed_join_refused:x');
    expect(errorText(new Error('boom'))).toBe('boom');
    expect(errorText({ message: 'Permission denied' })).toBe('Permission denied');
    expect(errorText({ code: 'camera_unavailable' })).toBe('camera_unavailable');
    expect(errorText({ detail: 1 })).toBe('{"detail":1}');
    expect(errorText(undefined)).toBe('undefined');
  });
});
