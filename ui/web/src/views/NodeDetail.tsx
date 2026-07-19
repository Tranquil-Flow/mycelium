import type { EvidenceLink, EvidenceNode } from '../model/types';

export interface NodeDetailProps {
  readonly node: EvidenceNode | null;
  readonly links?: readonly EvidenceLink[];
}

function format(value: number, digits = 1): string {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: digits }).format(value);
}

export function NodeDetail({ node, links = [] }: NodeDetailProps) {
  if (node === null) {
    return <p role="status">No device evidence selected.</p>;
  }
  const directedLinks = links.filter((link) => link.source === node.id || link.target === node.id);
  const location = node.location.state === 'known'
    ? `${node.location.city}, ${node.location.country}`
    : `Unknown location · ${node.location.reason.replaceAll('_', ' ')}`;
  return (
    <section aria-labelledby="node-detail-title">
      <div className="panel-titlebar compact">
        <div><span className="panel-kicker">Evidence device</span><h3 id="node-detail-title">{node.id}</h3></div>
      </div>
      <dl className="inspector-list">
        <div><dt>Location</dt><dd>{location}<small>{node.location.state === 'known' ? node.location.provenance : 'Not inferred'}</small></dd></div>
        <div><dt>Compute</dt><dd>{format(node.resources.gpuTeraflops)} GPU TFLOPS<small>{format(node.resources.cpuTeraflops)} CPU TFLOPS</small></dd></div>
        <div><dt>Memory</dt><dd>{format(node.resources.vramAvailableGb)} GB VRAM<small>{format(node.resources.ramAvailableGb)} GB RAM</small></dd></div>
        <div><dt>Directed links</dt><dd>{directedLinks.length}<small>Only supplied directions counted</small></dd></div>
      </dl>
      <div className="provenance-block">
        <span>Claim boundary</span>
        <strong><i aria-hidden="true" /> {node.provenance} evidence</strong>
        <p>Device evidence does not establish membership, assignment, qualification, or route readiness.</p>
      </div>
    </section>
  );
}

export default NodeDetail;
