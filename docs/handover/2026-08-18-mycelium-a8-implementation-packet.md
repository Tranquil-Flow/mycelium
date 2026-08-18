# Mycelium A8 Internet-native control implementation packet

**Status:** `design_only`

**Packet base:** `181dc07e06978dd45281434acf64460b61ea54a9`

**Machine source:** `tests/a8_acceptance/implementation_packet.v1.json`

**Protocol:** `mycelium.a8_implementation_packet.v1`

This is a design and acceptance handover for one future A8 leaf lane. It contains no
production implementation, shared wiring, external listener, provider deployment,
network contact, device use, physical evidence, qualification, readiness, or promotion.
Do not claim A8 implementation or qualification from this packet or its passing tests.

The packet binds the A8 specification and acceptance inventory to the concurrency and
integration ownership established by the A4 packet and completion plan. A8 may implement
the isolated leaf allowlist and run deterministic loopback/offline tests before A4 closes.
It may not touch a shared surface or begin physical preflight until the primary integrator
publishes one exact atomic A4 commit and the complete A8 leaf lane rebases onto it.

## 1. Exact default-deny leaf lane

The future leaf implementation may edit only these existing production patterns:

- `mycelium_seed/http.py`
- `mycelium_bootstrap_preflight/**`

It may edit only these existing test patterns:

- `tests/seed_coordinator/test_http.py`
- `tests/bootstrap_preflight/**`

It may introduce exactly one provider-neutral package, rooted at
`mycelium_internet_control`, with exactly these files:

- `mycelium_internet_control/__init__.py`
- `mycelium_internet_control/observation.py`
- `mycelium_internet_control/relay.py`

That package is a pure leaf. It owns closed nullable observation values, normalization of
detached `direct`/`relay`/`unknown` inputs, and privacy-reduced relay projection. It owns no
membership mutation, Iroh control, gateway dispatch, supervisor composition, generated
contract, UI projection, provider SDK, network I/O, or projection-key persistence.

Provider-neutral proxy/configuration templates may be introduced only in the exact new
directory `config/internet-bootstrap`, with exactly these paths:

- `config/internet-bootstrap/README.md`
- `config/internet-bootstrap/bootstrap.env.template`
- `config/internet-bootstrap/reverse-proxy.conf.template`

They remain inert examples. They contain no real origin, account identifier, certificate,
private key, credential, provider-specific API, or command that activates a listener.
Anything outside this allowlist is denied by default. Discovery of a required extra path
stops the leaf lane for primary-integrator review; the lane does not widen itself.

## 2. Explicitly prohibited ownership

The A8 leaf lane must not edit or assume ownership of:

- `mycelium_request_gateway/**`
- `mycelium_membership/contracts.py`
- `mycelium_seed/state.py`
- `mycelium_router/transports/iroh.py`
- `mycelium_live/supervisor.py`
- `mycelium_product_spine/**`
- `contracts/**` or `ui/contracts/**`
- `mycelium_ui_gateway/**` or `ui/**`
- `mycelium_physical_runner/**`, `tests/evidence/**`, or `docs/qualification/**`

This prohibition includes supervisor/shared composition, generated contracts and
compatibility fixtures, shared UI, external listeners, credentials and certificate
private keys, physical devices, shared fleet state, provider accounts/SDKs, and physical
or browser qualification evidence. Template text does not grant authority to bind a
non-loopback socket or configure real infrastructure.

## 3. Frozen HTTPS bootstrap boundary

The public address is one canonical `https://host[:port]` origin with no userinfo, path,
query, or fragment. Publicly trusted TLS authenticates only that hostname. Mycelium seed
authority remains the signed seed identity verified against the invitation-bound seed-key
pin before any invitation secret is transmitted. TLS success, a reused TLS connection,
an IP address, cookie, bearer token, or reverse-proxy decision grants no seed or member
authority.

The leaf seed listener is loopback HTTP behind a separately reviewed, still-inert TLS
proxy template. The exact public route matrix is closed:

| Method | Path | Authority |
| --- | --- | --- |
| `GET` | `/seed/identity` | signed seed identity |
| `GET` | `/seed/rotation` | dual-signed seed-key transition |
| `POST` | `/seed/join` | single-use invitation plus signed generation-zero join |
| `POST` | `/seed/resume` | signed current-member resume |
| `POST` | `/seed/message` | signed current-member message |

Every other method/path is a bounded error. Cleartext public origins, redirects,
downgrades, query/fragment data, form encoding, transfer encoding, websocket upgrade,
static files, directories, cookies/bearers as membership, and public operator
administration fail closed.

The future leaf uses these exact initial bounds:

- 1 MiB maximum canonical-JSON body;
- 2,000 ms connection read timeout;
- 32 concurrent requests by default, constrained to 1–256;
- 8 attempts per invitation in a 300,000 ms window by default, constrained to 1–32;
- 4 KiB maximum public error body.

Responses use exact `application/json`, canonical JSON, and `Cache-Control: no-store`.
Public failures disclose bounded reason codes only. Product evidence and public logging
exclude request bodies, queries, invitation identifiers/secrets, member IDs, EndpointIDs,
signatures, remote addresses, provider identifiers, and credentials.

## 4. Invitation, resume, and revocation authority

Invitation creation remains owner-private administration. `mycelium_seed/http.py` may
validate the closed transport envelope and delegate it once; it may not mint, consume, or
repair authority itself. The invitation secret exists only in the canonical JSON body of
`POST /seed/join`, after successful TLS hostname verification and signed seed identity/pin
verification. Atomic consumption, exact idempotent retry, and no-partial-member behavior
remain owned by the existing coordinator/state boundary outside the leaf lane.

Resume and later control are authorized by signed, current membership generations. IP,
TLS session state, cookies, and connection reuse are never ambient authority. The HTTP
adapter validates shape and delegates; it does not rewrite incarnation, generation,
lease, or revocation state.

Revocation has two effects with separate owners:

1. The leaf HTTP adapter rejects stale or revoked control when the existing membership
   authority reports it.
2. Closing or quarantining an already-open Iroh connection is later shared activation
   admission work owned by the transport/primary integrator.

The leaf package must not mutate `mycelium_seed/state.py`, membership contracts, or Iroh
admission to simulate the second effect.

## 5. Nullable observations and direct/relay truth

The only path classes are `direct`, `relay`, and `unknown`. The following values are
nullable: RTT, warm RTT, jitter, goodput, loss ratio, sample count, connection generation,
observation time, and freshness deadline. Missing, stale, rejected, or unmeasured values
remain null and the path class remains `unknown`; they are never coerced to zero.

Zero is valid only when a current explicit measurement has a positive sample count and
the measured quantity can truthfully be zero. Non-finite and negative measurements fail
closed. Unknown or stale values cannot satisfy a readiness/planning objective that
requires a measurement.

`direct` or `relay` is accepted only from a current bound live Iroh connection observation
for the exact membership-authorized EndpointID and connection generation. The leaf
package receives a detached provider-neutral observation from the later integrator. It
does not read Iroh configuration or control transport. Same LAN, different network, OS,
peer class, participation, address, hostname, a typed EndpointID, or a configured
force-relay switch cannot establish path class. Force-relay is a later physical test
control; the live connection observation is the evidence.

## 6. Privacy-reduced relay projection

The owner-private persistent projection key is injected later by shared composition and
is never generated, stored, or loaded by the leaf package. Relay references use:

`HMAC-SHA256(projection_key, "mycelium.relay_projection.v1\\0" || canonical_relay_identity)`

The same key, domain, and canonical identity produce the same reference; distinct relay
identities remain separated. The public result contains only `relay_reference` and an
optional coarse region. Region comes from reviewed relay metadata or explicit operator
declaration; otherwise it is unknown.

Raw relay URL, DNS name, IP address, port, credential, provider account, exact location,
and canonical relay identity cannot cross the public projection. The reference or region
never grants location, placement, route, membership, or qualification authority.

## 7. Deterministic acceptance only

Before A4 closes, A8 may execute only deterministic loopback/offline tests. The exact
future focused nodes are frozen in the machine packet. They cover:

- canonical HTTPS origin and closed public route/method behavior;
- seed pin/signature verification before invitation-secret transmission;
- join/resume/revocation delegation without partial mutation;
- request concurrency and per-invitation attempt limits;
- offline nullable infrastructure preflight with no physical-readiness claim;
- exact, inert, provider-neutral, secret-free template inventory;
- missing/stale/rejected measurements remaining unknown rather than zero;
- explicit measured-zero requirements;
- bound live-observation requirements for direct/relay; and
- stable, separated, privacy-reduced relay references with raw-identity rejection.

The negative inventory also fixes failures for invalid canonical origins, redirect or
downgrade, certificate success with bad seed authority, misplaced invitation secrets,
invalid/replayed/changed invitations, stale or revoked member generations, non-finite or
fabricated measurements, inferred/configured path labels, raw relay identity, contaminated
templates, network contact, external binds, shared-state mutation, and qualification
writes.

Packet and future leaf regression commands are:

```bash
python3.14 -m pytest -q tests/a8_acceptance
python3.14 -m pytest -q tests/seed_coordinator/test_http.py tests/bootstrap_preflight
ruff check tests/a8_acceptance mycelium_seed/http.py mycelium_bootstrap_preflight tests/seed_coordinator/test_http.py tests/bootstrap_preflight
```

These commands are not physical, browser, unrelated-network, direct-path, relay-path, or
qualification gates.

## 8. Unresolved shared surfaces

The following work is intentionally unresolved by this packet:

| Surface | Owner | Later handoff |
| --- | --- | --- |
| Invitation consumption, resume, leases, membership generation, revocation | membership and primary integrator | consume current authority until A4 rebase and explicit reservation |
| Open-connection revocation, activation admission, live Iroh observations | transport and primary integrator | wire detached observations and revocation after A4 rebase |
| Browser gateway and privacy-reduced public projection | gateway/UI integrator | wire only after contract and public-generation ownership is reserved |
| Supervisor composition, projection-key and configuration injection | primary integrator | inject after the exact A4 rebase |
| Contract generation and compatibility migration | contract integrator | generate only after leaf shapes and A4 interfaces stabilize |
| External listener, devices, fleet, physical/browser evidence | physical gate owner | reserve and execute only after rebase and shared-wiring review |

No leaf implementation may bridge these gaps through a local adapter, copied contract,
fixture, temporary UI field, test-only listener, credential, or physical assertion.

## 9. Exact later integration handoff

The later integration sequence is ordered and fail-closed:

1. The primary integrator publishes and verifies one exact atomic A4 commit.
2. The A8 leaf lane rebases its complete reviewed history onto that exact commit.
3. Run the packet checks and every deterministic loopback/offline leaf test again on the
   rebased tree.
4. The primary integrator reserves membership, transport, gateway, supervisor, contract,
   and shared UI surfaces.
5. The integrator wires existing membership authority and detached Iroh observations;
   those authorities do not move into `mycelium_internet_control`.
6. The contract integrator generates the closed contracts and compatibility fixtures only
   after leaf shapes stabilize.
7. Shared composition injects the owner-private persistent projection key and real
   provider-neutral configuration without committing credentials.
8. The physical owner runs external preflight, then unrelated-network join, direct,
   forced-relay, transition, reuse, reconnect, revocation, browser, privacy, and negative
   gates.
9. The owner reviews source-bound executed evidence before any A8 qualification,
   promotion, readiness, or completion claim.

If the exact A4 commit is unknown, disputed, non-atomic, or lacks its required ownership
interfaces, A8 stops. It does not guess the interface or qualify against this packet base.
