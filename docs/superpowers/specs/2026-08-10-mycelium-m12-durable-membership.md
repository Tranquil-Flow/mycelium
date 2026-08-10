# Mycelium M12 durable native membership specification

## Objective

Make every activation-eligible native device a durable device principal whose identity,
membership generation, incarnation, lease, and assignment binding survive ordinary
supervisor restarts. This replaces the live route's process-local seed signer without
creating a second membership implementation.

The implementation must reuse `mycelium_seed`, `mycelium_membership`, and
`mycelium_node.identity`. A user account is not a device identity and possession of an
invite does not imply tenant authorization, quota, or billing rights.

## Authorities and durable state

The seed coordinator owns one Ed25519 device identity and one SQLite state database
under a single owner-only state root:

- directory mode `0700`;
- identity and database mode `0600`;
- no symlink or non-regular-file traversal;
- one process lease for the state root;
- atomic creation and fsync before first use; and
- no private key, database bytes, invite nonce, or endpoint secret in logs, URLs,
  browser payloads, evidence exports, or fixtures.

The seed verification-key digest is the public coordinator identity. Restart with the
same state root must preserve it. Supplying an absent, replaced, corrupt, or
permission-unsafe state root fails closed before route construction.

## Device identity and lifecycle

Each native device owns a durable Ed25519 key independently of its hostname, IP,
Tailscale identity, user account, or current network. The signed join request binds:

- node ID and incarnation;
- endpoint ID and public endpoint record;
- peer class and exact runtime capability;
- invite nonce and swarm ID; and
- issued/expiry times.

Accepted membership creates a monotonically increasing membership generation and a
bounded lease. Heartbeats and activity receipts refresh liveness but never rewrite
identity. A restart increments incarnation while retaining the device key. Expired,
revoked, rotated, duplicate, stale-generation, wrong-endpoint, and wrong-swarm messages
are rejected.

Peer classes remain capability claims, not automatic stage eligibility:
`mac_mlx_iroh` may become activation eligible after complete evidence;
`browser_http` and the legacy `pixel_http` class remain Device Lab workers;
`android_termux_iroh` is a device-vendor-neutral experimental native activation
candidate backed by the Router/Iroh Android stdlib runtime (currently named
`pixel-stdlib` in code for compatibility); and `linux_tbd` remains ineligible until an
actual backend/transport contract exists. A Pixel 8 is the first conformance device,
not a special protocol target. Every Android/Termux candidate begins ineligible and
may run only explicitly labelled experimental operations until stage parity,
decode-mode, thermal, battery, background lifecycle, sleep, and network-loss
qualification passes.

## Signer rotation, restore, and loss

Planned seed rotation requires a signed transition record containing old/new public
key digests, generation, effective time, and operator reason. Both keys are accepted
only during a bounded overlap. Completion revokes the old key and increments the
membership authority generation.

A backup consists of the owner-only identity and SQLite database as one atomic
generation. Restore must verify modes, database integrity, key/database binding, and
monotonic generation before serving. Database-only or key-only restore is rejected.
Loss of both coordinator identity and backup creates a new swarm; it must never be
silently presented as continuity.

## Live route integration

The live supervisor receives an explicit seed state root. It loads the durable signer
once and passes it to membership-offer refresh. `_refresh_membership_snapshot` must not
generate a signer. Refreshed offers bind the same seed key digest, current clock,
membership generation, deployment epoch, graph digest, assignment digest, stage-pack
digest, load generation, and public peer endpoint records.

Route startup fails closed when the operator plan's seed digest disagrees with the
durable signer or when any offer has stale time, mixed generation, invalid signature,
unknown peer class, or endpoint mismatch. Existing plans without a durable state root
are accepted only behind an explicit legacy/test flag and can never qualify a live M12
deployment.

A graceful supervisor stop closes processes and transport but retains verified staged
artifacts. Explicit operator cleanup and failed-start cleanup remove partial/staged
state. Ordinary restart must not force model transfer; it reloads the same durable
identity, reuses digest-verified stage packs, and reruns the startup qualification.

## Product projection

The public snapshot exposes only pseudonymous member ID, peer class, lifecycle,
incarnation, membership generation, lease freshness class, runtime capability,
activation eligibility, revocation state, and public evidence digests. It never
exposes keys, endpoint addresses, hostnames, IPs, invite material, filesystem paths,
or operator usernames.

Nodes shows durable identity/freshness and capability. Network shows member/link
evidence separately from execution stages. Readiness explains membership exclusions.
Incidents records expiry, revocation, rotation, restore failure, and source loss with
stable codes. Settings may create/revoke bounded invites but never displays private
coordinator state.

## Acceptance gates

1. Unit/adversarial tests cover restart continuity, unsafe modes, symlinks, corruption,
   mismatched restore, stale/replayed messages, rotation overlap, revocation, and
   privacy.
2. Two supervisor restarts preserve the seed digest and enrolled native device IDs;
   incarnation and public source generations advance monotonically.
3. A revoked device disappears from activation eligibility and new work fails closed
   until a complete qualified topology exists.
4. A restored backup preserves identity; a key-only/database-only restore is rejected.
5. One Mac on a different ordinary LAN joins over the current private Tailscale
   bootstrap, renews its lease, survives restart, is revoked, and is restored without
   being counted as an inference stage.
6. The same facts appear after direct navigation and refresh in Network, Nodes,
   Readiness, Incidents, and Settings; fixture mode remains explicitly non-live.

The physical gate records both browser prompts, before/after frame counters, durable
seed digest, member generations/incarnations, lease events, revocation/restoration,
and exact test counts. Prompts and outputs remain only in the private Inference policy
lane.
