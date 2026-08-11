import { describe, expect, it } from 'vitest';
import { decodeModelCapacityRefreshStatus } from './modelCapacityRefresh';

const status = {
  protocol: 'mycelium.model_capacity_refresh.v1',
  generation: 3,
  state: 'succeeded',
  phase: null,
  started_at_unix_ms: 1_000,
  completed_at_unix_ms: 2_000,
  operation_digest: `sha256:${'a'.repeat(64)}`,
  catalog_generation: 77,
  evaluated_model_count: 5,
  reason_code: null,
  download_authorized: false,
  provisioning_started: false,
} as const;

describe('model capacity refresh contract', () => {
  it('decodes a bounded successful recheck', () => {
    expect(decodeModelCapacityRefreshStatus(status)).toEqual(status);
  });

  it('rejects hidden paths and inconsistent progress', () => {
    expect(() => decodeModelCapacityRefreshStatus({ ...status, cache_root: '/private/cache' })).toThrow(/shape/);
    expect(() => decodeModelCapacityRefreshStatus({ ...status, state: 'refreshing', phase: null })).toThrow(/state/);
    expect(() => decodeModelCapacityRefreshStatus({ ...status, download_authorized: true })).toThrow(/state/);
  });
});
