// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it, vi } from 'vitest';
import { scanWithPermission } from './api';

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
