# Mycelium M14 measured directed topology specification

## Objective and authority

M14 replaces a preselected physical device order with a deterministic directed-cycle
decision derived only from activation-plane Iroh observations. The selected cycle is
opened into an acyclic forward pipeline plus one explicit final-to-entry sampled-token
loopback. Layer allocation remains M13's contiguous dynamic program and executes only
after the topology order is frozen.

The selected route is serving authority only after assignment-local materialization,
load proof, canary qualification, and atomic promotion. Reachability probes, Tailscale
latency, fixture matrices, inferred reverse edges, and UI geometry are not topology
authority.

## Transport path observation

`mycelium.transport_path_observation.v1` is emitted by the native Iroh sidecar for
each configured outbound peer generation. Before an authenticated Iroh connection has
selected a path, `path_class` is `unknown`; it may become `direct` or `relay` only from
Iroh's selected path. The record binds:

- local and remote endpoint IDs and the configured peer generation;
- selected path class, relay identity when present, and relay region when known;
- cold and warm selected-path RTT, observed payload goodput, jitter, and loss;
- connections opened, frames sent, reconnects, selected-path changes, and sample
  window timestamps; and
- `measurement_source="iroh_activation_plane"`.

The sidecar keeps one authenticated QUIC connection per endpoint/generation and opens
new bidirectional streams on that connection. A frame counter greater than one with a
single connection is the required persistence proof. Failed connections are evicted,
increment reconnect evidence, and may be replaced; peer-generation rotation fences and
closes the old entry.

An accepted observation remains fresh for two hours after its final sample. This
bounded window permits measurement of both directed Hamiltonian cycles on a three-host
swarm and compilation of the winning route, while still failing closed against data
silently reused in a later operator session.

Path class is never inferred from route success, IP shape, generic reachability,
Tailscale, or a Router path manifest. A bounded physical transition exercise may try
to observe relay-to-direct or direct-to-relay. If the network does not produce one
within 60 seconds and 12 frames, the qualification records
`path_transition_not_observed_within_budget`; it must not synthesize a transition.

## Directed measurement matrix

Every ordered pair among at least three activation-eligible physical peers requires a
fresh observation. Each edge uses the same sidecar endpoint, peer generation,
connection, and selected path class used for Router activation traffic. The sample
window contains at least three successful frames. RTT is the sidecar's selected-path
round-trip estimate; topology uses `RTT / 2 + jitter`. Jitter is the population
standard deviation of selected-path RTT samples. Loss is failed attempts divided by
all attempts. Goodput is successful Router payload bytes divided by acknowledged
elapsed time. Reverse directions are measured independently and never mirrored.

`mycelium.link_state.v1` records projected from these observations carry the source
observation digest, endpoint IDs, connection generation, path class, sample count,
freshness deadline, formula identifier, and measured values. Missing, stale,
non-Iroh, unresolved-path, generation-mismatched, or endpoint-mismatched observations
are ineligible for pricing.

## Cycle search and opening

For three through seven peers, exact directed enumeration is the authority. Larger
fleets preserve the existing Held-Karp and bounded-heuristic modes and their honest
optimality labels. Candidate topology cost is the sum of directed one-way latency plus
jitter over every forward edge and the closing edge. Candidate evaluation records the
cycle, cost, explored count, mode, exactness, and rejection reason.

The selected cycle is rotated to the qualified entry host. The opened order feeds the
M13 layer allocator; no compute or memory coefficient may alter the already-frozen
topology order. Phase scoring uses activation payload size on forward edges and the
smaller sampled-token envelope on the decode closure. The execution graph must contain
only consecutive forward edges and one explicit final-to-entry loopback matching the
selected measured cycle.

## Product projection and UI

`mycelium.m14_topology_projection.v1` is a privacy-reduced projection shared by Plans,
Network, Nodes, and Readiness. It contains endpoint digests rather than private
addresses, the complete directed matrix, candidate cycles, selected/opened order,
forward and closure edge roles, path evidence, formulas, sample statistics, exclusions,
and promotion result. It never contains prompts, outputs, tokens, activations, tensor
contents, credentials, or private paths.

Network provides stable ring, SCC, elastic-geo, and true-map views. True-map preserves
unknown geometry as unknown and never invents coordinates. Directed physical rails are
visually distinct from logical forward and closure edges. Edge inspection shows the
symbolic transfer equation, substituted measured values, freshness, and whether each
term is measured or estimated. Plans shows every candidate, cost, explored count,
search mode/exactness, selected opening, allocation, and winning rationale.

## Acceptance gates

1. Contract and adversarial tests reject inferred or mirrored edges, stale samples,
   unresolved paths, incomplete ordered-pair matrices, endpoint/generation mismatch,
   non-Iroh sources, and one-connection-per-frame behavior.
2. Sidecar tests prove `unknown` before resolution, direct/relay classification from
   Iroh path state, path-change records, reconnect accounting, and multiple frames over
   one connection.
3. A complete six-edge directed matrix is captured from three physical peers and every
   edge has at least three successful activation-plane samples.
4. Exact search reports both candidate cycles, the measured winner, its cost, and
   whether it differs from canonical node-ID order without manufacturing a difference.
5. One real request advances both forward edges and the selected physical loopback;
   frame counters and connection identities bind the same run.
6. The promoted route survives two arbitrary browser prompts, navigation, and refresh.
   Plans, Network, Nodes, and Readiness explain the same topology result.
