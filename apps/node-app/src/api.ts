// SPDX-License-Identifier: AGPL-3.0-only
// The only module that talks to the native shell. Screens depend on the `NodeApi`
// interface so tests (and the future browser page) can supply another backend.
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';

export type NodeStatus = {
  phase: string;
  endpoint_id: string;
  relay_online: boolean;
  swarm_id: string | null;
  member_generation: number | null;
  lease_expires_at: number | null;
  /** True only while the seed-signed lease is unexpired. */
  lease_fresh: boolean;
  heartbeats_sent: number;
  last_heartbeat_at: number | null;
  last_error: string | null;
  stage_loaded: string | null;
  frames_received: number;
  frames_executed: number;
  last_execute_ms: number | null;
  peer_class: string;
  runtime_backend: string;
  route_ready: boolean;
  /** The owner-issued, seed-signed route assignment this node runs, if any. */
  route_assignment: string | null;
  route_assignment_error: string | null;
  /** v2 (digest-pinned) stage artifacts: `acquisition_pending` or `loaded`. */
  artifact_state?: string | null;
  /** The user chose to serve; a restarted app restores serving from it. */
  serving_intent: boolean;
};

export type ServiceStatus = { running: boolean; uptime_ms: number; mode: string };

/** Result of sharing the sealed evidence bundle (`mycelium.mobile_evidence_export.v1`). */
export type EvidenceExport = { shared: boolean; observations: number; downloads?: string | null };

export interface NodeApi {
  status(): Promise<NodeStatus>;
  pair(qr: string, deviceRef: string): Promise<NodeStatus>;
  join(): Promise<NodeStatus>;
  drain(): Promise<NodeStatus>;
  servingStart(): Promise<ServiceStatus>;
  servingStop(): Promise<ServiceStatus>;
  servingStatus(): Promise<ServiceStatus>;
  exportEvidence(): Promise<EvidenceExport>;
  onStatus(handler: (s: NodeStatus) => void): Promise<() => void>;
  /** The OS serving service changed (started, stopped from the notification, restored). */
  onService(handler: (s: ServiceStatus) => void): Promise<() => void>;
  scanQr(): Promise<string | null>;
  onDeepLink(handler: (url: string) => void): Promise<() => void>;
}

export const tauriApi: NodeApi = {
  status: () => invoke('node_status'),
  pair: (qr, deviceRef) => invoke('node_pair', { qr, deviceRef }),
  join: () => invoke('node_join'),
  drain: () => invoke('node_drain'),
  servingStart: () => invoke('serving_start'),
  servingStop: () => invoke('serving_stop'),
  servingStatus: () => invoke('serving_status'),
  exportEvidence: () => invoke('node_export_evidence'),
  onStatus: (handler) => listen<NodeStatus>('node://status', (e) => handler(e.payload)),
  onService: (handler) => listen<ServiceStatus>('node://service', (e) => handler(e.payload)),
  async scanQr() {
    return scanWithPermission(await import('@tauri-apps/plugin-barcode-scanner'));
  },
  async onDeepLink(handler) {
    const dl = await import('@tauri-apps/plugin-deep-link');
    const initial = await dl.getCurrent();
    initial?.forEach(handler);
    return dl.onOpenUrl((urls) => urls.forEach(handler));
  },
};

type ScannerModule = Pick<
  typeof import('@tauri-apps/plugin-barcode-scanner'),
  'checkPermissions' | 'requestPermissions' | 'scan' | 'Format'
>;

/**
 * Scan one QR code, asking for the camera first: Android only shows the camera
 * after a runtime grant, so a scan without one fails instead of prompting.
 */
export async function scanWithPermission(scanner: ScannerModule): Promise<string | null> {
  let state = await scanner.checkPermissions();
  if (state !== 'granted') state = await scanner.requestPermissions();
  if (state !== 'granted') {
    throw 'camera_permission_denied: allow the camera for Mycelium Node in Android settings, or paste the invite code instead';
  }
  const result = await scanner.scan({ formats: [scanner.Format.QRCode], windowed: false });
  return result?.content ?? null;
}

/**
 * Readable text for anything a command or plugin rejects with: core commands
 * reject with code strings, mobile plugins with objects ({ message }), and
 * String() of an object is the useless "[object Object]".
 */
export function errorText(e: unknown): string {
  if (typeof e === 'string') return e;
  if (e instanceof Error) return e.message;
  if (e && typeof e === 'object') {
    for (const key of ['message', 'error', 'code']) {
      const value = (e as Record<string, unknown>)[key];
      if (typeof value === 'string' && value) return value;
    }
    try {
      return JSON.stringify(e);
    } catch {
      // fall through
    }
  }
  return String(e);
}

/** Stable pseudonymous device label (never brand/model text). */
export function deviceRef(endpointId: string): string {
  return `device-${endpointId.slice(0, 12) || 'new'}`;
}
