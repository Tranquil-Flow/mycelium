# Request/Iroh local E2E qualification

Status: **RED — production cancellation is non-conformant.**

This report contains **local evidence only**. It does not establish physical,
remote-host, performance, privacy, or production qualification. The qualification
result for this report is always `route_ready=false`.

## Scope

The harness lives entirely in `tests/e2e_request_iroh/`. It does not modify or
replace production source. It runs this topology on one local host:

1. production `RequestGatewayASGIApplication` with `StaticBearerAuthenticator`;
2. production `RequestGatewayService` and exact qualification gate;
3. a locally produced `RouteQualificationV1` from the repository's signed,
   in-memory synthetic qualification fixture;
4. production `RouterSessionBackend`;
5. two production `Router` objects (`node-a`, `node-c`);
6. two production `IrohTransport` objects;
7. two native `mycelium-iroh-sidecar` child processes; and
8. a deterministic test model implementing `stage_local_kv` runtime semantics.

No in-process mesh, fake transport, Iroh loopback fallback, remote host,
production credential, package installation, or observatory write participates.
The native sidecars receive synthetic bootstrap secrets and the gateway uses a
synthetic bearer token. Cargo and sidecars receive allowlisted environment
variables rather than the parent credential environment. Sidecars run with
`--local-only`, and the harness rejects any advertised endpoint other than
`127.0.0.1` or `::1`. Iroh may still own an internal wildcard UDP socket, so
this local evidence does not establish firewall or network-namespace isolation.
The test model is intentionally harness-owned; routing, framing, sidecar
transport, authentication, qualification, admission, streaming,
acknowledgment, replay, and cancellation are production paths.

## TDD record

Focused RED was observed before harness implementation:

```text
python3.14 -m pytest -q tests/e2e_request_iroh/test_request_iroh_e2e.py
ERROR tests/e2e_request_iroh/test_request_iroh_e2e.py
ModuleNotFoundError: No module named 'tests.e2e_request_iroh.harness'
```

After implementing only owned harness files, the conformant slice is GREEN:

```text
python3.14 -m pytest -q tests/e2e_request_iroh/test_request_iroh_e2e.py -k 'not cancellation'
3 passed, 1 deselected
```

The focused cancellation proof remains RED against production behavior:

```text
python3.14 -m pytest -q \
  tests/e2e_request_iroh/test_request_iroh_e2e.py::test_cancellation_releases_gateway_adapter_and_all_router_resources

CancellationEvidence(
  gateway_released=True,
  adapter_released=False,
  entry_router_released=True,
  remote_router_released=False,
  pending_deliveries=0,
  local_evidence_only=True,
  route_ready=False,
)
```

## Proof matrix

| # | Required proof | Local result | Evidence |
|---|---|---|---|
| 1 | Reject unqualified request before Router mutation | PASS | Authenticated POST returns 409 `route_dropped`; ten Router/transport/runtime/capacity mutation counters remain unchanged. |
| 2 | Admit exactly one accepted request | PASS | Authenticated qualified POST returns 202; production Router port records one admission. |
| 3 | Prefill through production Router route and two native sidecars | PASS | Execution order is stages `(0, 1, 2)` over `node-a -> node-c`; transport class is `IrohTransport`; two distinct native sidecar PIDs run. |
| 4 | At least eight stage-local decode steps | PASS | Eight route steps, 24 runtime executions, exact order `(0, 1, 2) * 8`; each runtime requires live path-local state. |
| 5 | Token events return through adapter and acknowledged gateway stream | PASS | Router receives token indexes `0..8`; gateway emits and acknowledges them through production service subscriptions. |
| 6 | Stable activation, decode, and canonical token-frame digests | PASS | Two fresh complete topologies produce equal digest vectors: 3 activation, 24 decode payload, and 9 canonical token frames. |
| 7 | Resume from acknowledged cursor without duplicate emission | PASS | First stream acknowledges through cursor 1, observes token 1 without acknowledgment, closes, then resumes from cursor 1. Applied replay is exactly token indexes `1..8`; final token cursor is 9. |
| 8 | Cancellation releases Router, adapter, and gateway resources | **RED** | Gateway session payload/capture and entry Router path/capacity release. `RouterSessionBackend._cancelled` retains the request ID, and remote Router/runtime retain the registered path/state. Native pending deliveries reach zero. |
| 9 | Endpoint generation rotation rejects stale delivery | PASS | Delivery pauses after native receive but before dispatch; generation rotates `1 -> 2`; sender receives `peer_rotated`; router sees no stale token; pending deliveries reach zero. |
| 10 | Every report says local evidence only and `route_ready=false` | PASS | All three frozen evidence records contain `local_evidence_only=True` and `route_ready=False`; this document states the same scope. |

## Stable local evidence

Activation payload digest, repeated once per prefill stage:

```text
sha256:8d718944675f75da0c84ed8d5bf3e5f21402c37c844213ccd6d1d339ddcf1a6b
```

Decode payloads produce eight deterministic digests, each repeated once per
stage. Canonical token-frame digests produce nine deterministic values. The test
compares full vectors from two fresh native topologies rather than accepting
counts alone.

## Repository gates

All gates ran locally with network-dependent package installation disabled. This
is local evidence only and leaves `route_ready=false`.

| Gate | Result |
|---|---|
| Python 3.14 full suite | **RED:** 1 failed, 1005 passed, 2 skipped, 117 subtests passed. Sole failure is proof 8 above. |
| Focused conformant harness slice | GREEN: 3 passed, 1 deselected. |
| Rust format | GREEN: `cargo fmt --check`. |
| Rust lint | GREEN: `cargo clippy --all-targets --all-features -- -D warnings`. |
| Rust tests | GREEN: 21 passed across unit, capability, golden-wire, and sidecar-security tests. |
| Contract fixture/manifest checks and audit | GREEN: 14 fixtures verified; manifest verified; JSON audit `ok=true`. |
| Contract and wire tests | GREEN: 17 contract tests and 3 cross-language golden-wire tests passed. |
| Qualification and independent reference oracle | GREEN: 70 qualification and 24 oracle tests passed in separate invocations. |
| Python compileall | GREEN under an external temporary bytecode cache. |
| Existing-dependency UI check | GREEN: 70 UI tests and 3 contract-diff tests passed; typecheck and production build passed. No install ran. Package and lockfile digests matched the dependency-source checkout before use. |
| Release-security audit | GREEN on the explicitly staged owned files: no findings, `release_ready=false`, `route_ready=false`. |
| Claim-boundary audit | GREEN on the explicitly staged owned files: no findings, `release_ready=false`, `route_ready=false`. |
| Diff/ownership checks | GREEN: no whitespace errors; staged paths are exactly the five owned files. |

The UI build emitted only its existing chunk-size advisory. No commit is allowed
because the full Python gate and cancellation qualification remain RED.

## Production blocker

The minimized RED test identifies two release gaps without patching production:

1. `RouterSessionBackend._cancel_once()` inserts into `_cancelled`, but no
   lifecycle removes the request ID. This leaves an adapter tombstone after the
   gateway worker finishes.
2. Entry Router cancellation calls local `_cleanup_record()` and
   `relay.release_path()`, but no cancellation frame is sent through
   `IrohTransport` to remote Routers. The remote relay path and stage-local
   runtime state therefore remain active.

These observations are local evidence only. They make proof 8 fail and keep the
entire qualification RED with `route_ready=false`.

## Reproduction

All commands run from the isolated worktree. Cargo is forced offline:

```bash
export CARGO_NET_OFFLINE=true
PYTHONDONTWRITEBYTECODE=1 python3.14 -m pytest -q \
  tests/e2e_request_iroh/test_request_iroh_e2e.py
```

The complete suite is expected to report three passes and the single preserved
production RED above until production cancellation propagation and adapter
cleanup are implemented outside this harness-only change.
