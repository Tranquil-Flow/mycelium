# macOS A13 demo authority interface

Status: bounded local source contract for the macOS A13 hackathon demo. This is
not the normative A13 gate and does not qualify public Web-PKI or an unrelated
network.

## Construction and lifecycle

```python
from pathlib import Path
from mycelium_a13_macos.authority import DemoAuthority

authority = DemoAuthority(Path("~/Library/Application Support/Mycelium Demo"))
authority.start()
authority.create_swarm(
    {
        "cooperative_peer_visibility_acknowledged": True,
        "resource_use_acknowledged": True,
        "contribution_enabled": False,
    }
)
```

`state_root` is expanded to an absolute path and is created owner-private
(`0700`) if absent. An existing root must be a nonsymlink owner-owned `0700`
directory. `start()` is idempotent. It starts only a dynamically allocated
`127.0.0.1` TLS listener and, when durable swarm state exists, loads the seed
key named by SQLite metadata with `load_bound_seed_signer`. It never creates a
swarm implicitly. `stop()` closes the owned listener and drops in-memory
objects while retaining all keys, SQLite state, encrypted recovery, accepted
membership and consent records. Starting an enrolled installation resumes the
canonical member over pinned local HTTPS and atomically advances its durable
membership generation. Start fails closed if an expected identity is missing
or changed; it never regenerates a previously established permanent identity.

`create_swarm(consent: dict)` requires exactly the three boolean fields shown
above and both acknowledgement fields must be true. It creates a fresh durable
seed authority and the local target's permanent Ed25519 identity before any
invite can be minted. It returns `status()`. Calling it for an existing swarm
fails closed.

`status()` returns only the closed privacy-reduced record:

```text
protocol, started, swarm_created, local_tls, membership_state,
member_count, membership_generation, lease_state, contribution_enabled,
route_ready
```

It contains no state path, origin/hostname/port, node/Endpoint ID, key or key
digest, invite, pairing nonce/key, ciphertext, quota, address, prompt or model
data. `route_ready` is always false in this authority slice; enrollment is
membership only.

## Pairing and membership

```python
request = authority.pair_request()
handoff = authority.approve_pairing(
    request,
    ttl_seconds=120,
    quota={
        "storage_bytes": 2_000_000_000,
        "network_bytes_per_day": 5_000_000_000,
        "background_allowed": False,
    },
)
accepted = authority.accept_pairing(handoff)
renewed = authority.renew()
revoked = authority.revoke(node_id, generation)
```

`pair_request()` returns a signed, closed
`mycelium.pairing_request.v1` document containing the target node ID, permanent
Ed25519 public record/digest, peer class, supported invite protocol, ephemeral
X25519 public key, random nonce, and bounded issue/expiry times. It contains no
swarm credential. An unexpired pending request is returned byte-for-byte on
retry; its permanent identity is never regenerated. Once accepted, new pairing
is refused.

`approve_pairing(request, ttl_seconds, quota)` verifies the target's permanent
Ed25519 signature, request lifetime and supported class, checks the exact quota
shape above (nonnegative bounded integer byte limits plus a boolean background
flag), and uses the live canonical `SeedCoordinator.mint_invite`. The invite
nonce is the signed pairing nonce. It returns only a closed
`mycelium.encrypted_invitation_handoff.v1` document. X25519 + HKDF-SHA256 derives
one recipient-only key; ChaCha20-Poly1305 authenticates a handoff binding the
request digest, target identity, nonce, class, expiry, quota, seed pin and
complete canonical A8 invite bundle. No plaintext invite is returned or stored.
The approved expiry may not exceed either the pairing request or invite.

`accept_pairing(handoff)` first durably stores only the recipient-bound
ciphertext in an owner-only recovery file. It verifies/decrypts locally, checks
all request/recipient/identity/nonce/class/expiry/quota/seed-pin bindings, then
uses a pinned-certificate `SeedHTTPClient` over the actual loopback TLS socket
to fetch and verify the canonical signed seed identity and submit the canonical
join request. The signed join envelope is checkpointed before transmission so
an uncertain retry reproduces exactly the same request. The canonical SQLite
seed transaction owns invite consumption and accepted generation. Canonical
`save_membership_state` atomically persists target restart state before
recovery is removed. Reaccepting the exact completed handoff returns the
existing outcome; changed handoff retry fails closed.

`renew()` sends a canonical signed heartbeat through the same pinned HTTPS
client and accepts the signed lease renewal. It preserves membership generation.
`revoke(node_id, generation)` calls the live canonical coordinator generation
fence with lifecycle `STOPPED` and returns a privacy-reduced receipt. Later
renew/resume fails closed. The caller obtains `node_id` from its private pairing
request or underlying member object, not from `status()`.

## Integration properties

These read-only properties expose the real runtime objects for the parent
serving composition; each is `None` when unavailable/stopped:

- `authority.coordinator`: live canonical `mycelium_seed.SeedCoordinator`;
- `authority.member`: live canonical `mycelium_node.NodeMembershipSession`;
- `authority.seed_server`: TLS wrapper whose `.canonical_server` is the live
  canonical `mycelium_seed.http.SeedHTTPServer`;
- `authority.seed_client`: live pinned-certificate canonical
  `mycelium_seed.http.SeedHTTPClient`.

The properties are an integration seam, not a second authority. Parent serving
code must continue to derive placement, artifact, load, challenge,
qualification and selection from their canonical owners. This module grants
none of those states.

## Trust and claim boundary

The certificate and private key are generated once into this installation's
owner-private state and reused across restart. The client explicitly pins that
local certificate while canonical seed signatures remain authoritative. This
is real HTTPS transport and real SQLite/signature behavior for one installation,
but it is **local TLS**, not public trust, public exposure, unrelated-network
onboarding, serving qualification, or full A13 completion. `PublicBootstrapClient`
remains unchanged and is never told that loopback TLS is publicly trusted.
