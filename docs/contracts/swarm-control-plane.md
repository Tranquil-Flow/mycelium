# Swarm control plane

Mycelium has one membership plane. `SeedCoordinator` is the transport-agnostic
authority for signed admission, node identity, lease and membership generation.
`SqliteSeedState` is its durable store. Browser HTTP, Pixel HTTP, and Iroh are
adapters over that plane, not separate swarms.

## Peer classes

| Peer class | Runtime | Transport | May join membership | Activation eligible |
|---|---|---|---|---|
| `mac_mlx_iroh` | MLX | Iroh | Yes | Yes |
| `browser_http` | Browser | HTTP | Yes | No |
| `pixel_http` | Android | HTTP | Yes | No |
| `linux_tbd` | Undecided | None | Yes | No |

The seed derives activation eligibility from the exact signed peer-class and
runtime-capability declaration. A transport adapter cannot promote its peer.
In particular, a browser may contribute browser-stage evidence and matrix
results, but `assignment_offer()` rejects it as either an activation recipient
or activation peer.

## Browser adapter

`mycelium_interactive.swarm.SwarmCoordinator` preserves the browser-facing API:
fragment-only invitation URLs, origin validation, stage-pack delivery, hidden
matrix validation, long polling, result submission, cancellation, leave,
revocation, and sanitized status.

The adapter implements admission through the seed:

1. `create_invite()` mints the seed's Ed25519-signed invitation and retains only
   its digest, nonce, and monotonic transport deadline.
2. `exchange_invite()` constructs a `browser_http` membership session, sends
   the existing signed `mycelium.membership.join_request.v1` envelope through
   `SeedCoordinator.accept_join()`, and returns the existing signed
   `mycelium.membership.join_acceptance.v1` envelope with the browser grant.
3. The seed rejects reuse of a browser or Mac `node_id` with a different key or
   endpoint. Browser and Mac namespaces therefore cannot diverge or collide.
4. A restarted adapter enumerates `browser_http` members from the restored seed
   store. Membership and its generation survive; bearer session tokens do not.

`InteractiveRuntime` constructs that seed once from its state root. It uses
`load_or_create_node_signer(<root>/seed/identity/seed.key)` and places both
`SqliteInviteRegistry` and `SqliteSeedState` on
`<root>/seed/state.sqlite3`; `SeedCoordinator` binds the database to the swarm,
seed node, and signer identity. The established identity/storage helpers enforce
mode `0600` for the key and database and mode `0700` for their private
directories. Restart continuity therefore applies when the same explicit state
root is reused. An implicit temporary runtime root remains intentionally
ephemeral.

The adapter-generated browser membership signer is process-local and has no
serialization-facing private-key API. This refactor does not change browser
page key storage or any signed membership wire schema.

## Authority and generation fencing

The browser session token is only an HTTP transport credential. Possessing it
does not establish current membership. Every authenticated browser operation
also resolves the seed member and requires the peer class and membership
generation to match the adapter's admitted generation, the authoritative lease
to remain live, and lifecycle state to be one of `NEW`, `CONFIGURED`, or
`RUNNING`. `DRAINING`, `STOPPING`, `STOPPED`, unknown lifecycle states, and
expired leases fail closed.

When work is assigned, the job snapshots that membership generation.
`start_work()` and `submit_result()` reject a job after the seed advances the
member generation, even if the caller still presents the original valid bearer
token. Result acceptance and identical-duplicate completion run inside a
`SeedCoordinator` authority guard: the guard checks class, generation, lease,
lifecycle, and durable current-member state, then retains the seed lock through
the adapter's completion commit. Generation advancement therefore linearizes
either before that guard and is rejected, or after the accepted completion;
there is no acceptance gap between the final authority check and commit.

The adapter always acquires its condition before seed authority. The seed guard
does not acquire adapter locks or call into the adapter, and seed operations
never invert that order, so concurrent revocation cannot form a lock cycle.
`revoke_peer()` advances the durable generation and records `STOPPING`;
`leave()` advances it and records `STOPPED`. The seed update occurs before the
adapter changes local transport state or releases in-flight work.

Recovered membership is visible in status and evidence, but an old bearer token
is unauthorized after adapter restart. Transport credential continuity is not
membership authority.

## Browser compatibility boundary

Browser work and result documents retain their existing protocols and
stage-matrix bindings. Assignment ID, stage ID, stage-pack digest, input digest,
output digest, cancellation state, and request binding still fail closed.
Status adds peer class, activation eligibility, and membership generation; it
does not expose invite tokens, bearer tokens, stage tensors, or hidden matrices.
Raw invite and bearer credentials are not added to durable seed state.

## Claim boundary

The interactive browser path remains local qualification evidence:

- work, result, status, and stage packs require `route_ready=false`;
- status remains `local_evidence_only=true`;
- a `browser_http` member is activation-ineligible;
- signed membership proves admission and identity, not a physical inference
  route or production readiness.

Physical Iroh activation, cross-device execution, sealed route evidence, and a
qualification-authority decision are separate remaining work. Only that
physical qualification path may produce `route_ready=true`.
