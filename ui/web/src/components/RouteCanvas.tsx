import { useEffect, useMemo, useState } from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  ReactFlow,
  type Edge,
  type NodeTypes,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { layoutGraph, projectRouteGraph, type GraphLayout, type SceneGraph } from '../graph/graph';
import type { EvidenceRoute, EvidenceSnapshot } from '../model/types';
import { DeviceNode, type DeviceFlowNode } from './DeviceNode';

interface RouteCanvasProps {
  readonly snapshot: EvidenceSnapshot;
  readonly route: EvidenceRoute;
  readonly layout: GraphLayout;
  readonly selectedNodeId: string;
  readonly onNodeSelect: (nodeId: string) => void;
}

const nodeTypes: NodeTypes = { device: DeviceNode };

function handoffLabel(edge: SceneGraph['edges'][number]): string {
  if (edge.kind === 'decode_closure') {
    return 'DECODE CLOSURE';
  }
  if (edge.link === null) {
    return 'STAGE HANDOFF · LINK UNKNOWN';
  }
  return `HANDOFF · ${edge.link.metrics.roundTripTimeMs.value.toFixed(1)} MS RTT`;
}

export function RouteCanvas({
  snapshot,
  route,
  layout,
  selectedNodeId,
  onNodeSelect,
}: RouteCanvasProps) {
  const [scene, setScene] = useState<SceneGraph | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setScene(null);
    setError(null);

    const graph = projectRouteGraph(snapshot, route.id);
    void layoutGraph(graph, layout)
      .then((nextScene) => {
        if (!cancelled) setScene(nextScene);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : 'Unknown graph layout error');
        }
      });

    return () => {
      cancelled = true;
    };
  }, [layout, route.id, snapshot]);

  const nodes = useMemo<DeviceFlowNode[]>(
    () =>
      scene?.nodes.map((node) => ({
        id: node.id,
        type: 'device',
        position: { x: node.x, y: node.y },
        data: { sceneNode: node },
        draggable: false,
        selectable: true,
        selected: node.id === selectedNodeId,
      })) ?? [],
    [scene, selectedNodeId],
  );

  const edges = useMemo<Edge[]>(
    () =>
      scene?.edges.map((edge) => {
        const closure = edge.kind === 'decode_closure';
        return {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          type: closure ? 'default' : 'smoothstep',
          selectable: true,
          focusable: true,
          label: handoffLabel(edge),
          className: closure ? 'route-edge decode-edge' : 'route-edge handoff-edge',
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 16,
            height: 16,
            color: closure ? '#a999c8' : '#8fe34f',
          },
          style: closure
            ? { stroke: '#a999c8', strokeWidth: 1.4, strokeDasharray: '7 6' }
            : { stroke: '#8fe34f', strokeWidth: 1.6 },
          labelStyle: {
            fill: closure ? '#c4b8dc' : '#b5dd91',
            fontSize: 9,
            fontWeight: 700,
            letterSpacing: '0.06em',
          },
          labelBgStyle: { fill: '#151020', fillOpacity: 0.96 },
          labelBgPadding: [5, 3] as [number, number],
          labelBgBorderRadius: 2,
        };
      }) ?? [],
    [scene],
  );

  const unknownLocations = scene?.nodes.filter((node) => node.locationUnknown).length ?? 0;

  if (error !== null) {
    return (
      <div className="canvas-state canvas-error" role="alert">
        <span className="state-symbol" aria-hidden="true">!</span>
        <div>
          <strong>Route layout unavailable</strong>
          <p>{error}</p>
        </div>
      </div>
    );
  }

  if (scene === null) {
    return (
      <div className="canvas-state" role="status" aria-live="polite">
        <span className="layout-loader" aria-hidden="true" />
        <div>
          <strong>Computing deterministic layout</strong>
          <p>Projecting fixture stages; no live peers are queried.</p>
        </div>
      </div>
    );
  }

  return (
    <section className="route-canvas" aria-label={`${layout} route graph, read only`}>
      {layout === 'geo' && unknownLocations > 0 ? (
        <div className="unknown-location-notice" role="status">
          {unknownLocations} unknown location{unknownLocations === 1 ? '' : 's'} placed in evidence tray
        </div>
      ) : null}
      <ReactFlow<DeviceFlowNode, Edge>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        edgesReconnectable={false}
        deleteKeyCode={null}
        onNodeClick={(_event, node) => onNodeSelect(node.id)}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1.15 }}
        minZoom={0.35}
        maxZoom={1.5}
        panOnScroll
        selectionOnDrag={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#34284b" gap={24} size={1} variant={BackgroundVariant.Dots} />
        <Controls showInteractive={false} position="bottom-left" />
      </ReactFlow>
      <div className="edge-legend" aria-label="Graph edge legend">
        <span><i className="legend-line handoff" aria-hidden="true" /> Stage handoff</span>
        <span><i className="legend-line closure" aria-hidden="true" /> Decode closure</span>
      </div>
    </section>
  );
}
