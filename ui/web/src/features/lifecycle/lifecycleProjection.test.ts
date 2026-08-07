import { describe, expect, it } from 'vitest';
import {
  QUALIFIED_LIFECYCLE_READY_STATES,
  isReadyLifecycleState,
  projectLifecycle,
  projectLifecycleFromSources,
  type LifecycleInputs,
  type LifecycleState,
} from './lifecycleProjection';
import {
  peerLostFixture,
  preparingFixture,
  peerLostNativeBrowserFixture,
  staleFixture,
  revokedFixture,
  cleanupFixture,
  qualifiedReadyFixture,
  loadingFixture,
  generatingFixture,
  cancellingFixture,
  recoveringFixture,
  unreadyFixture,
} from '../../test/lifecycleFixtures/recordedSnapshots';

const LIFECYCLE_STATES: readonly LifecycleState[] = [
  'preparing',
  'loading',
  'unready',
  'qualified-ready',
  'generating',
  'cancelling',
  'peer-lost',
  'recovering',
  'stale',
  'revoked',
  'cleanup-complete',
];

describe('lifecycleProjection', () => {
  describe('lifecycle state union', () => {
    it.each(LIFECYCLE_STATES)('renders %s as a known state with a label and accessibility text', (state) => {
      const projection = projectLifecycle(preparingFixture({ state }));
      expect(projection.state).toBe(state);
      expect(projection.label.length).toBeGreaterThan(0);
      expect(projection.accessibility_text.length).toBeGreaterThan(0);
    });

    it('treats qualified-ready as the only state that can ever be inference-enabled', () => {
      expect(QUALIFIED_LIFECYCLE_READY_STATES).toEqual(['qualified-ready']);
      for (const state of LIFECYCLE_STATES) {
        if (state === 'qualified-ready') {
          expect(isReadyLifecycleState(state)).toBe(true);
        } else {
          expect(isReadyLifecycleState(state)).toBe(false);
        }
      }
    });
  });

  describe('preparing', () => {
    it('is shown only while qualifier evidence has not yet been accepted', () => {
      const inputs = preparingFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('preparing');
      expect(projection.route_ready).toBe(false);
      expect(projection.inference_enabled).toBe(false);
      expect(projection.block_reason).not.toBeNull();
      expect(projection.label).toMatch(/preparing/i);
    });
  });

  describe('loading', () => {
    it('is shown while a snapshot is in flight with no qualifier evidence yet', () => {
      const inputs = loadingFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('loading');
      expect(projection.route_ready).toBe(false);
      expect(projection.inference_enabled).toBe(false);
      expect(projection.label).toMatch(/loading/i);
    });
  });

  describe('unready', () => {
    it('is shown when qualifier exists but route_ready is false', () => {
      const inputs = unreadyFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('unready');
      expect(projection.route_ready).toBe(false);
      expect(projection.inference_enabled).toBe(false);
      expect(projection.qualifier_authority).toBe(true);
      expect(projection.label).toMatch(/unready|qualifier-owned not accepted/i);
    });
  });

  describe('qualified-ready', () => {
    it('is shown only when qualifier owns route_ready=true with physical evidence class', () => {
      const inputs = qualifiedReadyFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('qualified-ready');
      expect(projection.route_ready).toBe(true);
      expect(projection.inference_enabled).toBe(true);
      expect(projection.qualifier_authority).toBe(true);
      expect(projection.evidence_class).toBe('physical_qualification');
      expect(projection.label).toMatch(/qualified/i);
    });

    it('does not claim ready when a synthetic fixture is presented, even with route_ready=true', () => {
      const inputs = qualifiedReadyFixture({ force_synthetic_route_ready: true });
      const projection = projectLifecycle(inputs);
      expect(projection.state).not.toBe('qualified-ready');
      expect(projection.route_ready).toBe(false);
      expect(projection.inference_enabled).toBe(false);
    });
  });

  describe('generating', () => {
    it('is shown while an accepted request is actively streaming or submitting without enabling new submissions', () => {
      const inputs = generatingFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('generating');
      expect(projection.route_ready).toBe(true);
      expect(projection.inference_enabled).toBe(false);
      expect(projection.phase).toBe('streaming');
      expect(projection.accepted_request?.request_id).toBe('request-streaming');
      expect(projection.qualified_binding).toBe(inputs.inference.qualification?.binding);
    });
  });

  describe('cancelling', () => {
    it('is shown while a cancellation is pending and disables new submissions', () => {
      const inputs = cancellingFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('cancelling');
      expect(projection.route_ready).toBe(false);
      expect(projection.inference_enabled).toBe(false);
      expect(projection.phase).toBe('cancelling');
      expect(projection.label).toMatch(/cancel/i);
    });
  });

  describe('peer-lost', () => {
    it('is shown when a required native node or browser peer disappears from the swarm', () => {
      const inputs = peerLostFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('peer-lost');
      expect(projection.route_ready).toBe(false);
      expect(projection.inference_enabled).toBe(false);
      expect(projection.lost_peer_ids.length).toBeGreaterThan(0);
      expect(projection.label).toMatch(/peer lost|peer-lost/i);
    });

    it('detects browser worker disappearance from the live swarm status snapshot', () => {
      const inputs = peerLostNativeBrowserFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('peer-lost');
      expect(projection.lost_peer_ids).toContain('browser-1');
    });
  });

  describe('recovering', () => {
    it('is shown after peer-lost when the qualifier has re-issued binding evidence', () => {
      const inputs = recoveringFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('recovering');
      expect(projection.route_ready).toBe(false);
      expect(projection.inference_enabled).toBe(false);
      expect(projection.previous_lost_peer_ids.length).toBeGreaterThan(0);
      expect(projection.label).toMatch(/recover/i);
    });
  });

  describe('stale', () => {
    it('is shown when the latest qualification digest is older than the freshness window', () => {
      const inputs = staleFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('stale');
      expect(projection.route_ready).toBe(false);
      expect(projection.inference_enabled).toBe(false);
      expect(projection.label).toMatch(/stale/i);
    });
  });

  describe('revoked', () => {
    it('is shown when a native node or browser worker has been revoked in the swarm', () => {
      const inputs = revokedFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('revoked');
      expect(projection.route_ready).toBe(false);
      expect(projection.inference_enabled).toBe(false);
      expect(projection.revoked_member_ids.length).toBeGreaterThan(0);
      expect(projection.label).toMatch(/revoked/i);
    });
  });

  describe('cleanup-complete', () => {
    it('is shown only after every requested cleanup endpoint has succeeded', () => {
      const inputs = cleanupFixture();
      const projection = projectLifecycle(inputs);
      expect(projection.state).toBe('cleanup-complete');
      expect(projection.route_ready).toBe(false);
      expect(projection.inference_enabled).toBe(false);
      expect(projection.cleanup_complete).toBe(true);
      expect(projection.label).toMatch(/cleanup/i);
    });

    it('does not claim cleanup-complete when any cleanup task is still pending', () => {
      const base = cleanupFixture();
      const partial: LifecycleInputs = {
        ...base,
        swarm: {
          ...base.swarm,
          cleanup: {
            ...base.swarm.cleanup,
            leave_confirmed: false,
            session_cleared: true,
          },
        },
      };
      const projection = projectLifecycle(partial);
      expect(projection.state).not.toBe('cleanup-complete');
      expect(projection.cleanup_complete).toBe(false);
    });
  });

  describe('claim boundary', () => {
    it('never enables new inference outside the qualified-ready state', () => {
      for (const state of LIFECYCLE_STATES) {
        const projection = projectLifecycle(preparingFixture({ state }));
        if (state === 'qualified-ready') {
          expect(projection.route_ready).toBe(true);
          expect(projection.inference_enabled).toBe(true);
          continue;
        }
        if (state === 'generating') {
          expect(projection.route_ready).toBe(true);
          expect(projection.inference_enabled).toBe(false);
          continue;
        }
        expect(projection.route_ready).toBe(false);
        expect(projection.inference_enabled).toBe(false);
      }
    });

    it('never lets fixture source mode claim qualified-ready even with physical route_ready evidence', () => {
      const inputs: LifecycleInputs = {
        ...qualifiedReadyFixture(),
        observatory: { ...qualifiedReadyFixture().observatory, kind: 'fixture' },
        swarm: { ...qualifiedReadyFixture().swarm, kind: 'fixture' },
        inference: { ...qualifiedReadyFixture().inference, kind: 'fixture' },
      };
      const projection = projectLifecycle(inputs);
      expect(projection.state).not.toBe('qualified-ready');
      expect(projection.route_ready).toBe(false);
      expect(projection.inference_enabled).toBe(false);
    });

    it('never claims a real device — it only reflects recorded event or swarm data', () => {
      const projection = projectLifecycle(qualifiedReadyFixture());
      expect(projection.claim_boundary).toBe('recorded_event_projection_only');
      expect(projection.real_device).toBe(false);
      expect(projection.physical_devices_present).toBe(0);
    });
  });

  describe('source projection', () => {
    it('composes the state from observatory, swarm, and inference inputs without inventing data', () => {
      const inputs: LifecycleInputs = {
        observatory: {
          kind: 'live',
          source_cursor: 5,
          observed_at_unix_ms: 1_700,
          qualification: null,
          incidents: [],
          sessions: [],
        },
        swarm: {
          kind: 'live',
          native_nodes: [],
          browser_workers: [],
          previous_browser_worker_ids: [],
          previous_native_member_ids: [],
          revoked_member_ids: [],
          cleared_at_unix_ms: null,
          cleanup: {
            leave_confirmed: false,
            session_cleared: false,
          },
        },
        inference: {
          kind: 'live',
          qualification: null,
          qualification_loading: true,
          phase: 'idle',
          accepted_request: null,
          freshness_window_ms: 5_000,
          now_unix_ms: 1_000,
        },
      };
      const projection = projectLifecycleFromSources(inputs);
      expect(projection.state).toBe('loading');
      expect(projection.route_ready).toBe(false);
      expect(projection.qualifier_authority).toBe(false);
    });

    it('prefers recorded lifecycle state from observatory when provided explicitly', () => {
      const projection = projectLifecycleFromSources(
        preparingFixture({ state: 'peer-lost' }),
      );
      expect(projection.state).toBe('peer-lost');
    });
  });
});
