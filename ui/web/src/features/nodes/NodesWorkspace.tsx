import { useMemo, useState } from 'react';
import type { EvidenceSnapshot, ProvisioningEvidence } from '../../model/types';
import {
  filterAndSortNodes,
  projectNodeInventory,
  redactedNodeDetail,
  type NodeInventoryItem,
  type NodeSortKey,
} from './nodeInventory';

export interface NodesWorkspaceProps {
  readonly snapshot: EvidenceSnapshot;
  readonly provisioning: ProvisioningEvidence;
}

function readiness(state: string): string {
  return state.replaceAll('_', ' ');
}

export function NodesWorkspace({ snapshot, provisioning }: NodesWorkspaceProps) {
  const inventory = useMemo(() => projectNodeInventory(snapshot, provisioning), [snapshot, provisioning]);
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<NodeSortKey>('id');
  const [direction, setDirection] = useState<'asc' | 'desc'>('asc');
  const [selected, setSelected] = useState<NodeInventoryItem | null>(null);
  const visible = useMemo(
    () => filterAndSortNodes(inventory, { query, key: sort, direction }),
    [direction, inventory, query, sort],
  );
  const setOrdering = (key: NodeSortKey) => {
    if (key === sort) setDirection((current) => current === 'asc' ? 'desc' : 'asc');
    else { setSort(key); setDirection('asc'); }
  };
  const detail = selected === null ? null : redactedNodeDetail(selected);
  return (
    <div className="view nodes-workspace">
      <header className="view-heading"><div><p className="eyebrow cyan">Evidence inventory</p><h2>Nodes</h2><p className="view-description">Simulation and artifact-provisioning identities remain separate unless mapping evidence is supplied.</p></div></header>
      <section className="panel" aria-label="Node inventory controls">
        <label>Search nodes<input type="search" aria-label="Search nodes" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      </section>
      <div className="table-scroll">
        <table className="strategy-table" aria-label="Node inventory">
          <thead><tr>
            <th scope="col"><button type="button" onClick={() => setOrdering('id')}>Sort by node</button></th>
            <th scope="col"><button type="button" onClick={() => setOrdering('scope')}>Scope</button></th>
            <th scope="col"><button type="button" onClick={() => setOrdering('memory')}>Memory</button></th>
            <th scope="col"><button type="button" onClick={() => setOrdering('assignment')}>Assignment</button></th>
            <th scope="col"><button type="button" onClick={() => setOrdering('readiness')}>Readiness</button></th>
            <th scope="col">Inspect</th>
          </tr></thead>
          <tbody>{visible.map((node) => <tr key={node.key}>
            <th scope="row">{node.alias}</th>
            <td>{node.scope.replaceAll('_', ' ')}</td>
            <td>{node.memory.availableGb === null ? 'UNKNOWN' : `${node.memory.availableGb.toFixed(1)} GB`}</td>
            <td>{node.assignment?.exactRange ?? 'UNASSIGNED'}</td>
            <td><span>Artifacts <strong>{readiness(node.readiness.artifactsVerified)}</strong></span><br/><span>Runtime <strong>{readiness(node.readiness.runtimeLoaded)}</strong></span></td>
            <td><button type="button" aria-label={`Inspect node ${node.alias}`} onClick={() => setSelected(node)}>Inspect node</button></td>
          </tr>)}</tbody>
        </table>
      </div>
      <p className="claim-boundary">Simulation and artifact-provisioning identities are not merged; no cross-scope identity mapping is established.</p>
      {detail === null ? null : <section className="panel" role="region" aria-label="Node detail">
        <h3>{detail.identity.displayId}</h3><p>{detail.redaction}</p>
        <dl className="inspector-list">
          <div><dt>Scope</dt><dd>{detail.identity.scope}</dd></div>
          <div><dt>Identity mapping</dt><dd>{detail.identity.mapping}</dd></div>
          <div><dt>Assignment</dt><dd>{detail.assignment?.exactRange ?? 'UNKNOWN'}</dd></div>
          <div><dt>Artifacts verified</dt><dd>{readiness(detail.readiness.artifactsVerified)}</dd></div>
          <div><dt>Runtime loaded</dt><dd>{readiness(detail.readiness.runtimeLoaded)}</dd></div>
          <div><dt>Stage probed</dt><dd>{readiness(detail.readiness.stageProbed)}</dd></div>
          <div><dt>Route ready</dt><dd>{readiness(detail.readiness.routeReady)}</dd></div>
        </dl>
      </section>}
    </div>
  );
}

export default NodesWorkspace;
