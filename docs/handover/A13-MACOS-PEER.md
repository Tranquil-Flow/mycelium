# A13 macOS remote TARGET member handover

Status: **locally verified target enrollment/lifecycle component**. This is a
host-level local TLS/SQLite integration result, not a two-Mac physical run,
serving qualification, public-network deployment, or complete macOS A13 demo.
The two targets in the tests are independent local instances; no physical or
fault-domain inference may be drawn from them.

## Owned paths

- `mycelium_a13_macos/peer.py`
- `tests/a13_macos/test_peer.py`
- `docs/handover/A13-MACOS-PEER.md`
- `outputs/a13-macos-evidence/peer-*.log`
- `outputs/a13-macos-evidence/peer-verification.json`

No existing source or test file was modified by this component. Nothing was
committed, staged, pushed, deployed, exposed publicly, or run on a remote Mac.

## API contract

```python
from mycelium_a13_macos.peer import RemoteMacMember

member = RemoteMacMember(
    "~/Library/Application Support/Mycelium Demo/target",
    timeout=10,
)
member.start()  # creates/loads the target-owned Ed25519 key before a request
request = member.pair_request(
    {
        "cooperative_peer_visibility_acknowledged": True,
        "resource_use_acknowledged": True,
        "contribution_enabled": False,
    },
    ttl_seconds=300,
)
# Transfer request to the owner; receive recipient-encrypted handoff only.
accepted = member.accept_handoff(handoff)
renewed = member.renew()
capability = member.capability_report(
    platform="macos",
    architecture="arm64",
    memory_bytes=16 * 1024**3,
    available_storage_bytes=100 * 1024**3,
    backends=["mlx"],
    precisions=["float32"],
)
stopped = member.stop()  # retains identity, membership and private evidence
```

### Construction and transport

`RemoteMacMember(state_root, *, clock=time.time, timeout=10,
_test_seed_client_factory=None)` expands the target root to an absolute path.
The root and subdirectories are owner-only `0700`; identity and canonical JSON
state/evidence files are `0600`. On the production-default path,
`_test_seed_client_factory` is absent: the implementation uses the normal
canonical `SeedHTTPClient`, the system Web-PKI trust store, canonical HTTPS-only
origins, no redirects, bounded canonical JSON, and invite-pinned signed seed
identity verification before transmitting the invitation token.

`_test_seed_client_factory` is deliberately private/test-named. The tests inject
an installation-local CA only through that argument to reach an actual dynamic
`127.0.0.1` TLS socket. A negative test proves the default client does not trust
that local test CA. Injection never disables HTTPS, origin equality, seed-key
pin, signed identity, pairing, membership, or canonical transport checks.

The target never constructs `SeedCoordinator`, `SqliteSeedState`, or an invite
registry, and never creates a seed directory or SQLite file. In the local
integration fixture, only the separately owned authority root has SQLite.

### Lifecycle methods

- `start()` atomically creates the durable target identity on first use. If
  accepted membership exists, it performs an invite-free canonical resume with
  the same identity. A restart generation is checkpointed before send and saved
  through `save_membership_state` only after signed acceptance.
- `pair_request(consent, ttl_seconds=300)` requires the exact three consent
  booleans, with resource use and cooperative-peer visibility both explicitly
  acknowledged. An unexpired request is returned exactly across retries and
  process restart; the target identity never changes. The request has no swarm
  credential.
- `accept_handoff(handoff)` stores recipient ciphertext as private recovery,
  decrypts with existing X25519/HKDF/ChaCha20-Poly1305 pairing code, revalidates
  request/recipient/identity/nonce/class/expiry/quota/invite/seed bindings,
  verifies the pinned signed seed identity over HTTPS, checkpoints an exact
  signed join, and calls the canonical `/seed/join`. Accepted membership is
  atomically saved before pairing recovery is erased. Exact uncertain join and
  resume retries replay the checkpoint byte-for-byte; changed handoffs fail.
- `renew()` sends a canonical signed heartbeat and accepts its signed lease
  renewal without changing membership generation.
- `capability_report(...)` signs and submits the existing canonical capability
  message, privately retaining the report and seed receipt. This is a resource
  observation only; it does not qualify a model, assignment, route, or host.
- `stop()` drops only live client/session objects. It deliberately retains the
  target-owned key, accepted membership, consent, and private evidence.
- `member` and `seed_client` expose the live canonical objects for the parent
  serving composition. They are `None` before enrollment, after `stop()`, or
  after a failed resume.

Canonical unknown, locally expired, revoked, and stale-generation members fail
closed as `peer_member_unknown`, `peer_member_expired`, `peer_member_revoked`,
and `peer_generation_stale`. The last definitive disposition is persisted
privately and projected without identifiers or credentials.

### Status contract

`status()` returns exactly:

```text
protocol, started, identity_state, pairing_state, membership_state,
membership_generation, lease_state,
cooperative_peer_visibility_acknowledged, resource_use_acknowledged,
contribution_enabled, capability_state, serving_state, route_ready
```

It never returns a state path, node/Endpoint ID, identity/seed key digest, seed
origin, pairing nonce/key, invitation token, handoff ciphertext, quota, prompt,
activation, or model data. Membership and serving are independent:
`membership_state == "current"` can coexist only with
`serving_state == "not_yet_serving"` and `route_ready == False` in this slice.

## TDD and verification receipts

Commands were run from the explicit absolute repository directory with the
lane `.venv`, isolated Python environment, and pytest plugin autoload disabled.

Intended RED (missing production module):

```bash
cd /Users/evinova-self/Documents/playground/mycelium-a13-macos-demo
umask 077
mkdir -p outputs/a13-macos-evidence
chmod 700 outputs/a13-macos-evidence
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python -B -m pytest -q tests/a13_macos/test_peer.py \
  2>&1 | tee outputs/a13-macos-evidence/peer-red.log
```

Observed: collection failed with `ModuleNotFoundError: No module named
'mycelium_a13_macos.peer'`; exit 2. This is the intended missing-component RED.

Focused final GREEN:

```bash
cd /Users/evinova-self/Documents/playground/mycelium-a13-macos-demo
umask 077
set -o pipefail
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python -B -m pytest -q tests/a13_macos/test_peer.py \
  2>&1 | tee outputs/a13-macos-evidence/peer-green.log
```

Observed: `9 passed in 1.97s`; exit 0.

Applicable macOS A13 regression GREEN:

```bash
cd /Users/evinova-self/Documents/playground/mycelium-a13-macos-demo
umask 077
set -o pipefail
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python -B -m pytest -q tests/a13_macos \
  2>&1 | tee outputs/a13-macos-evidence/peer-a13-green-rerun.log
```

Observed: `66 passed in 14.07s`; exit 0.

An earlier aggregate attempt is retained at `peer-a13-green.log`: an existing
`DemoAuthority.pair_request()` random-nonce issue produced one
`pairing_request_invalid` failure after 61 passes, then the failed existing test
passed alone and the complete aggregate rerun above passed. This component does
not own or modify `pairing.py`/`authority.py`; its request path supplies a
valid-alphabet prefixed random nonce and is covered by the final runs.

The focused tests exercise actual local TLS sockets, canonical HTTP requests,
canonical membership signatures, real authority SQLite rows, two independent
target keys, no target SQLite/seed directory, exact request/join/resume retry,
restart generation accounting, response-loss recovery, post-accept crash
cleanup, renew, capability submission, stop retention, default Web-PKI refusal
of the test CA, consent rejection, tamper/wrong-target rejection,
unknown/expired/revoked/stale rejection, private modes, and status leak canaries.

## Remaining production network and product requirements

1. A separately owned seed publisher must expose the canonical seed routes at
   the invite's stable public HTTPS origin with a publicly trusted certificate,
   correct signed `seed_url`, and no redirect/downgrade. This component neither
   provisions nor publishes that listener.
2. The remote Mac must have DNS and outbound HTTPS reachability to that exact
   origin. Public unrelated-network/Web-PKI operation remains unexecuted here.
3. The parent serving layer must replace this membership-only `.invalid`
   endpoint advertisement with the real authenticated activation-plane endpoint
   when it is ready, then perform assignment, artifact authorization, load,
   startup challenge, qualification, selection, drain, and serving lifecycle.
4. Two separate real Macs are still required by current owner direction for the
   physical demo. No remote access, installation, reservation, listener, model,
   inference, browser path, or physical fault/restart test was authorized or
   performed by this component.
5. Model/revision/representation, host assignment, run count, resource/byte/cost
   limits, and evidence destination require explicit approval before any heavy
   or physical inference work.

Suggested owner commit title: `Add remote mac target enrollment lifecycle`

Suggested description: `Add a target-only RemoteMacMember using durable device
identity, recipient-encrypted pairing, canonical pinned HTTPS membership,
restart-safe generation checkpoints, renewal/capability lifecycle, private
status, and real local TLS/SQLite TDD coverage.`
