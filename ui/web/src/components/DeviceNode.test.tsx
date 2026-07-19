import { render, screen } from '@testing-library/react';
import { ReactFlowProvider } from '@xyflow/react';
import { describe, expect, it } from 'vitest';
import { DeviceNode } from './DeviceNode';
import type { SceneNode } from '../graph/graph';
import type { EvidenceNode } from '../model/types';

const evidenceNode = {
  id: 'physical-only', location: { state: 'unknown', provenance: 'unknown', reason: 'not_provided' }, provenance: 'synthetic',
  resources: { gpuTeraflops: 0, cpuTeraflops: 0, vramAvailableGb: 0, ramAvailableGb: 0, gpuMemoryBandwidthGbps: 0, ramBandwidthGbps: 0, vramRamBandwidthGbps: 0, unifiedMemory: false, workspaceGb: 0 },
} satisfies EvidenceNode;
const sceneNode = {
  id: 'device:physical-only', kind: 'device', order: 1, routeId: 'route-a', nodeId: 'physical-only', stageId: null,
  stage: null, evidenceNode, location: evidenceNode.location, routeRole: 'unassigned', x: 0, y: 0, width: 100, height: 70,
  startBoundary: null, endBoundary: null, layerSpanWidth: null, locationUnknown: true, tray: 'physical-only', anchorX: null, anchorY: null, clusterId: null,
} satisfies SceneNode;

describe('DeviceNode', () => {
  it('renders physical-only devices without fabricating stage evidence', () => {
    render(<ReactFlowProvider>{DeviceNode({ data: { sceneNode } } as never)}</ReactFlowProvider>);
    expect(screen.getByText('physical-only')).toBeInTheDocument();
    expect(screen.getByText(/unassigned device/i)).toBeInTheDocument();
    expect(screen.queryByText(/ready/i)).not.toBeInTheDocument();
  });
});
