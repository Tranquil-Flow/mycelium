import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import type { SceneNode } from '../graph/graph';

export interface DeviceNodeData extends Record<string, unknown> {
  readonly sceneNode: SceneNode;
}

export type DeviceFlowNode = Node<DeviceNodeData, 'device'>;

function locationLabel(node: SceneNode): string {
  if (node.location.state === 'unknown') {
    return 'Unknown location';
  }
  return `${node.location.city}, ${node.location.country}`;
}

export function DeviceNode({ data, selected }: NodeProps<DeviceFlowNode>) {
  const node = data.sceneNode;
  const endLayer = node.stage.endLayerExclusive - 1;
  const memoryMode = node.evidenceNode.resources.unifiedMemory ? 'Unified memory' : 'Discrete GPU';

  return (
    <article className={`device-node${selected ? ' is-selected' : ''}${node.locationUnknown ? ' is-unknown' : ''}`}>
      <Handle type="target" position={Position.Left} className="device-handle" />
      <div className="device-node-topline">
        <span className="stage-index">S{String(node.order + 1).padStart(2, '0')}</span>
        <span className="node-health"><i aria-hidden="true" /> modeled</span>
      </div>
      <strong className="device-name">{node.nodeId}</strong>
      <span className="device-location">{locationLabel(node)}</span>
      <div className="device-node-metrics">
        <span>L{node.stage.startLayer}–{endLayer}</span>
        <span>{node.stage.layerCount} layers</span>
        <span>{memoryMode === 'Unified memory' ? 'UMA' : 'GPU'}</span>
      </div>
      <Handle type="source" position={Position.Right} className="device-handle" />
    </article>
  );
}
