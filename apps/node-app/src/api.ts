// SPDX-License-Identifier: AGPL-3.0-or-later
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
};

export type ServiceStatus = { running: boolean; uptime_ms: number; mode: string };

export interface NodeApi {
  status(): Promise<NodeStatus>;
  pair(qr: string, deviceRef: string): Promise<NodeStatus>;
  join(): Promise<NodeStatus>;
  drain(): Promise<NodeStatus>;
  servingStart(): Promise<ServiceStatus>;
  servingStop(): Promise<ServiceStatus>;
  servingStatus(): Promise<ServiceStatus>;
  onStatus(handler: (s: NodeStatus) => void): Promise<() => void>;
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
  onStatus: (handler) => listen<NodeStatus>('node://status', (e) => handler(e.payload)),
  async scanQr() {
    const scanner = await import('@tauri-apps/plugin-barcode-scanner');
    const result = await scanner.scan({ formats: [scanner.Format.QRCode], windowed: false });
    return result?.content ?? null;
  },
  async onDeepLink(handler) {
    const dl = await import('@tauri-apps/plugin-deep-link');
    const initial = await dl.getCurrent();
    initial?.forEach(handler);
    return dl.onOpenUrl((urls) => urls.forEach(handler));
  },
};

/** Stable pseudonymous device label (never brand/model text). */
export function deviceRef(endpointId: string): string {
  return `device-${endpointId.slice(0, 12) || 'new'}`;
}
