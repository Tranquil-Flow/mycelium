# Placement provenance and signed peer identity

`placement_provenance` is a required, signed control-plane field. It identifies the authority that selected a placement; it does not grant route-readiness by itself.

## Accepted values

| Value | Authority | When valid |
|---|---|---|
| `offline_capacity_planner` | The offline capacity planner operating on its bound membership/capability snapshot | Initial placement and any operator-requested offline replan |
| `seed_emergency_replacement` | The seed coordinator | Replacement only after an existing placement is invalidated by expiry, liveness loss, or revocation |

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

The seed may broker, sign, persist, and revoke membership and assignment records. It does not become a general route planner. `seed_emergency_replacement` is limited to replacing an invalidated placement; normal placement continues to come from the offline planner.
