# Mycelium Gossip Evidence Plane

Status: standalone, pre-integration MVP implementation.

This subsystem discovers peers, disseminates device/runtime/network evidence, tracks freshness and liveness, and projects immutable consumer views. It does not select routes, assign layers, reserve work, execute inference, or import router/allocator/planner/simulator code.

## Isolation boundary

Owned files:

- `mycelium_gossip/`
- `tests/gossip/`
- `GOSSIP_PROTOCOL.md`

Protected existing components remain unchanged. Integration should happen only after their contracts stabilize.

Dependency direction after integration:

```text
probe/runtime callbacks
        |
        v
NodeStateSession -> RecordEnvelope -> GossipService -> VersionedRecordStore
                                           |                  |
                                      transport callbacks     v
                                           |          immutable snapshot
                                           v                  |
                               InMemory or Zenoh backend       v
                                                    RouterView / AllocatorView
                                                             |
                                                 existing consumers import views
```

The evidence plane never imports existing consumers. Consumers may later import frozen view types or receive them through an adapter owned by the integration session.

## Modules

- `schema.py`: strict versioned payload contracts, envelope validation, canonical hash, logical/transport keys.
- `codec.py`: bounded canonical JSON decoder/encoder; rejects duplicate keys, NaN/Inf, excessive depth, excessive collections, and oversized records.
- `state.py`: stable node ID, atomically incremented/fsynced incarnation, exclusive process lock, ephemeral boot ID, independent per-record sequences.
- `registry.py`: origin-owned version registers, replay/equivocation detection, TTL expiry events, cardinality limits, immutable snapshots.
- `transport.py`: `GossipTransport` protocol, deterministic in-memory mesh, bounded/coalescing callback inbox.
- `service.py`: subscription/query startup, anti-entropy repair thread, liveness state machine, partition recovery challenge, scoped quarantine, events, diagnostics.
- `views.py`: frozen router and allocator evidence projections; no scoring or decisions.
- `zenoh_transport.py`: optional lazy-loaded Zenoh backend.

Base package uses the Python standard library. Zenoh remains optional.

## Trust boundary

MVP assumes explicitly approved peers on a trusted LAN or private overlay. `payload_hash` detects corruption/equivocation but is not a signature and does not authenticate a peer.

Never publish:

- API keys, auth keys, passwords, tokens, private keys;
- exact location;
- user documents or local model paths;
- secrets embedded in `extensions`.

Decoder and schema enforce known secret field-name rejection. Cryptographic node identity, ACLs, and hostile-Sybil defenses remain post-MVP work.

## Envelope

Protocol: `mycelium.gossip.record.v1`.

Required fields:

```json
{
  "protocol": "mycelium.gossip.record.v1",
  "swarm_id": "swarm-a",
  "kind": "profile",
  "origin_node_id": "node-a",
  "incarnation": 4,
  "sequence": 9,
  "boot_id": "boot-example",
  "generated_at_unix_ms": 1784150000000,
  "ttl_ms": 60000,
  "payload_hash": "sha256:...",
  "payload": {}
}
```

Ordering uses `(incarnation, sequence)`, never wall time. Wall time supports diagnostics only. Duplicate delivery never extends local freshness.

Record kinds:

- `profile`: relatively static hardware/software/endpoints/policy.
- `status`: volatile resource, reservation, thermal, queue, and performance evidence.
- `offering`: assignment-bound loaded runtime capability.
- `link`: directed endpoint-pair reachability/performance evidence.
- `membership`: observer evidence; local Zenoh liveliness remains a separate signal.

Schema versions:

- `mycelium.device_profile.v2`
- `mycelium.device_status.v1`
- `mycelium.runtime_offering.v1`
- `mycelium.link_state.v1`
- `mycelium.membership.v1`

Layer ranges are half-open: `[start_layer, end_layer_exclusive)`.

## Key space

```text
mycelium/<swarm>/<origin>/profile
mycelium/<swarm>/<origin>/status
mycelium/<swarm>/<origin>/offering/<deployment>/<assignment>/<endpoint>
mycelium/<swarm>/<origin>/link/<src-endpoint>/<dst-node>/<dst-endpoint>
mycelium/<swarm>/<origin>/membership/<subject-node>
mycelium/<swarm>/liveness/<node>/<incarnation>/<boot-id>
```

Transport key fields must exactly match envelope fields. A link key identifies one directed endpoint pair; LAN, private-overlay, relay, and HTTP paths never overwrite each other.

## Identity lifecycle

Use a dedicated private state directory:

```python
from mycelium_gossip.state import open_node_state

with open_node_state("~/.mycelium/gossip/node-state.json") as identity:
    print(identity.node_id, identity.incarnation, identity.boot_id)
```

Rules:

1. First open generates stable random node ID and persists incarnation 1.
2. Every later open increments and fsyncs incarnation before networking.
3. Process holds an exclusive lock for session lifetime.
4. Concurrent process with same identity fails closed.
5. Boot ID changes each process start.
6. Per-logical-record sequence begins at zero each incarnation.
7. Corrupt or symlinked state fails closed without replacement.

## Freshness and registry behavior

Each accepted record stores local monotonic `accepted_at` and `expires_at` values. TTL expiry is an active semantic transition even when no packet arrives:

```text
fresh -> expired -> excluded from consumer snapshots
```

The old version remains as a bounded watermark, preventing delayed duplicate resurrection. Only a higher sequence or incarnation can refresh it.

Registry bounds:

- maximum origin peers;
- maximum logical records;
- maximum directed links per origin;
- immutable snapshots;
- callback failures isolated and counted.

Apply outcomes include accepted, duplicate, stale, conflict, identity conflict, capacity rejected, and wrong swarm.

## Liveness

Local state machine:

```text
unknown -> alive -> suspect -> dead
                      |
                      +-- same-incarnation PUT remains suspect
```

Zenoh liveliness DELETE means suspect, not unquestioned global death. Suspicion becomes dead after configured local grace.

Same-incarnation partition recovery requires all of:

1. liveliness PUT returns;
2. application challenge succeeds with issued nonce;
3. fresh status accepted after suspicion began.

Delayed PUT alone cannot resurrect peer. Older-incarnation DELETE cannot kill newer process. Two boot IDs claiming same node/incarnation produce `identity_conflict`.

## Intake and anti-entropy

Transport callbacks only enqueue. Registry/view mutation occurs on worker thread.

Inbox behavior:

- separate priority lane for liveliness/control events;
- records coalesced by logical key;
- latest pending record replaces older pending record;
- hard queue limits and drop counters;
- priority drained before ordinary records.

Startup order:

1. open transport;
2. subscribe records;
3. subscribe liveliness with history;
4. query bounded snapshot;
5. ingest through normal registry path;
6. declare local liveliness;
7. start worker and independent repair thread.

Periodic query repair runs outside event worker, so blocked query cannot delay liveness or active-route failure handling.

## Scoped failure feedback

Runtime/data-plane failures use `FailureObservation` with:

- route ID and route generation;
- source/destination node and endpoint IDs;
- optional offering/assignment ID;
- failure kind and probe correlation ID;
- scope: `edge`, `offering`, or `peer`.

Every report immediately emits `ActiveRouteAtRisk`. Default quarantine remains narrow:

- edge failure quarantines endpoint pair;
- runtime/model failure quarantines offering;
- application failure quarantines peer only when explicitly scoped as peer.

Gossip records evidence; router later chooses whether/how to recompute.

## Consumer views

Build views without importing existing decision code:

```python
from mycelium_gossip.views import build_allocator_view, build_router_view

snapshot = service.registry.snapshot()
peers = service.peer_states_snapshot()
quarantines = service.quarantine_snapshot()
router_view = build_router_view(snapshot, peers, quarantines)
allocator_view = build_allocator_view(snapshot, peers, quarantines)
```

These three source snapshots are independently captured. The resulting views are
deeply immutable, but the bundle is not a transactionally atomic cross-source
snapshot: peer state or quarantine state can change between calls. Integration
code must capture/retry around source generations (or use a future service-owned
atomic capture API) before acting on a route. `snapshot_generation` currently
identifies the registry generation only.

`RouterView` contains:

- node eligibility and exclusion reasons;
- endpoint-specific directed edges;
- ready assignment-bound offerings;
- per-record evidence versions;
- snapshot and eligibility generations.

It contains no selected route or score.

`AllocatorView` contains:

- reservation-aware memory domains;
- allocatable, committed, and reclaimable bytes;
- concurrency and queue evidence;
- peer eligibility/exclusion reasons.

Unified CPU/GPU memory appears once through a shared memory-domain ID.

## Zenoh deployment

`ZenohTransport` imports Zenoh only when `start()` runs. Importing Mycelium gossip without Zenoh installed works.

Safety defaults:

- loopback listener;
- multicast disabled;
- wildcard listeners rejected unless explicitly allowed;
- 4-second lease, four keepalives;
- explicit approved connect/listen endpoints.

Zenoh 1.9 peer regions require clique connectivity for peer-to-peer data routing. For manual/private-overlay deployment, each peer region must form full mesh. A star can propagate some liveliness while failing B-to-C record delivery. Do not treat liveliness convergence as proof of data convergence.

LAN multicast scouting can form clique automatically. Router-mode Zenoh may become appropriate later for larger or non-clique deployments; not part of brokerless MVP.

## Standalone verification

Dependency-free suite:

```bash
python3 -m pytest tests/gossip -q
```

Real Zenoh suite, from a disposable environment containing `eclipse-zenoh==1.9.0`:

```bash
python -m pytest \
  tests/gossip/test_zenoh_transport.py \
  tests/gossip/test_zenoh_integration.py \
  tests/gossip/test_zenoh_multiprocess.py -q -s
```

Qualification covers:

- canonical schema/codec rejection paths;
- version/replay/expiry semantics;
- bounded callback intake;
- startup query repair;
- blocked-repair priority isolation;
- same-incarnation recovery;
- scoped quarantine;
- immutable views;
- direct-connect real Zenoh pub/sub/query/liveliness;
- three-service explicit clique convergence;
- five independent multicast-discovered processes;
- periodic anti-entropy;
- abrupt SIGKILL to suspect transition.

## Integration gate

Do not edit router, allocator, planner, simulator, legacy broadcaster, or existing probe until their current sessions finish.

Later integration should add only an adapter owned by that integration change:

1. map existing probe/runtime output into strict payloads;
2. hand frozen views to consumers;
3. map runtime failures into `FailureObservation`;
4. run shadow mode where gossip route inputs are compared but not acted upon;
5. enable route recomputation only after parity/chaos gates.

Still required before production deployment:

- cross-host LAN test;
- broader private-overlay deployment test under loss/load (the two-peer explicit-
  endpoint Tailscale qualification passed on 2026-07-16; evidence:
  `.hermes/evidence/gossip/2026-07-16_111727-physical-tailscale-zenoh-attempt2/`);
- 10% loss and partition test;
- sleep/wake test;
- inference-under-load CPU/RSS/latency test;
- Linux and Windows wheel parity;
- longer soak;
- approved-interface and locator-disclosure audit.
