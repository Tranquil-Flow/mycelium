# Placement provenance and signed peer identity

`placement_provenance` is a required, signed control-plane field. It identifies the authority that selected a placement; it does not grant route-readiness by itself.

## Accepted values

| Value | Authority | When valid |
|---|---|---|
| `frozen_fixture` | Checked-in, digest-bound demo assignment | Phase-3 proof path only; immutable after the first physical run is sealed |
| `planner_v2` | Capacity-aware placement planner | Product placement compiled from its bound membership and evidence snapshot |

No other value is accepted. An operator, node agent, browser peer, or transport adapter cannot relabel a placement.

## Signed assignment binding

Every `mycelium.membership.assignment_offer.v1` message carries:

- `placement_provenance`;
- `peer_endpoint_records`, including the signed node ID, authenticated EndpointID, deployment epoch, membership generation, and validity interval for every peer the recipient may exchange activations with.

The node agent configures transport identity only through `NodeMembershipSession.resolve_peer_endpoint()`. Operator-supplied expected EndpointIDs are rejected. Missing, expired, superseded-generation, or endpoint-conflicting records fail closed before transport connection.

Only peers whose signed join declaration is the exact `mac_mlx_iroh` runtime capability are currently eligible for activation assignments. `browser_http`, `pixel_http`, and `linux_tbd` members may join the same membership plane but are not activation-stage eligible.

## Qualification evidence binding

The physical route challenge carries `placement_provenance` verbatim. The evidence manifest hashes the complete route-challenge file. `RouteQualificationV1` repeats the accepted value, and the qualifier rejects missing or non-enumerated values.

Changing provenance after sealing changes the route-challenge digest and causes `evidence_file_digest_mismatch`. Therefore a sealed run cannot be relabeled from offline planning to emergency replacement, or vice versa, without producing a new evidence manifest and qualification record.

## Authority boundary

The seed brokers, signs, persists, and revokes membership and assignment records; it does not become a route planner. `frozen_fixture` identifies the precommitted proof assignment. `planner_v2` is emitted only by the planner-backed placement source introduced after the seam exists.
