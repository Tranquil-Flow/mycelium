from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping, Optional

from .contracts import DirectedLinkObservation, NodeCapability, PlanningPolicy
from .network_cost import transfer_time_ms


@dataclass(frozen=True)
class PhysicalGraph:
    nodes: Mapping[str, NodeCapability]
    edges: Mapping[tuple[str, str], DirectedLinkObservation]
    adjacency: Mapping[str, tuple[str, ...]]
    exclusions: Mapping[str, str]

    @property
    def candidate_node_ids(self) -> tuple[str, ...]:
        return tuple(self.nodes)

    def link(self, src: str, dst: str) -> Optional[DirectedLinkObservation]:
        return self.edges.get((src, dst))

    def require_link(self, src: str, dst: str) -> DirectedLinkObservation:
        link = self.link(src, dst)
        if link is None:
            raise ValueError(f"missing directed edge {src}->{dst}")
        return link


def build_physical_graph(
    nodes: Iterable[NodeCapability],
    links: Iterable[DirectedLinkObservation],
    policy: PlanningPolicy,
) -> PhysicalGraph:
    all_nodes: dict[str, NodeCapability] = {}
    exclusions: dict[str, str] = {}
    for node in sorted(nodes, key=lambda item: item.node_id):
        if node.node_id in all_nodes or node.node_id in exclusions:
            raise ValueError(f"duplicate node id: {node.node_id}")
        if not node.eligible:
            exclusions[node.node_id] = node.exclusion_reason or "ineligible"
        else:
            all_nodes[node.node_id] = node

    edge_map: dict[tuple[str, str], DirectedLinkObservation] = {}
    for link in sorted(links, key=lambda item: (item.src, item.dst)):
        if link.src not in all_nodes or link.dst not in all_nodes:
            continue
        key = (link.src, link.dst)
        if key in edge_map:
            raise ValueError(f"duplicate directed link: {link.src}->{link.dst}")
        try:
            transfer_time_ms(link, 0, policy)
        except ValueError:
            continue
        edge_map[key] = link

    adjacency = {
        node_id: tuple(dst for (src, dst) in edge_map if src == node_id)
        for node_id in all_nodes
    }
    return PhysicalGraph(
        nodes=MappingProxyType(dict(sorted(all_nodes.items()))),
        edges=MappingProxyType(dict(sorted(edge_map.items()))),
        adjacency=MappingProxyType(adjacency),
        exclusions=MappingProxyType(dict(sorted(exclusions.items()))),
    )
