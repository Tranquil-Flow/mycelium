# Physical MVP local verification handover

## Claim boundary

This tranche proves hardware-independent lifecycle behavior and local native-Iroh process behavior only.

- `route_ready=false`
- `release_ready=false`
- No sealed physical qualification was produced.
- No multi-host physical Router/MLX/Iroh route was executed.
- The process-rebirth probe replaces a native Iroh sidecar on one Linux host. It is not evidence that a remote physical inference-node process recovered.
- The negative-run harness uses deterministic synthetic qualification fixtures. Its records are not physical evidence and cannot authorize promotion.

## Implemented lifecycle behavior

The production Router recovery path now:

1. Releases the failed attempt and rebuilds a higher path attempt.
2. Encodes prompt plus committed token IDs with the canonical token payload codec.
3. Dispatches `RECOVERY_PREFILL` through the configured transport instead of the local whole-manifest executor.
4. Preserves recovery phase, token index, topology, idempotency, and source identity across progressive hops.
5. Accepts final `ManifestLocked` only when request, graph, manifest, path attempt, final-hop source, and manifest validation all match the recovering record.
6. Transitions the recovered request through `PREFILL -> LOCKED -> DECODING`.
7. Cleans up failed lock registration and dispatch failures without duplicate runtime cancellation.

The local native-Iroh rebirth probe proves that:

- tokens are emitted before failure;
- the old sidecar PID exits;
- a replacement sidecar has a new PID and EndpointID;
- both peer bindings advance from generation 1 to generation 2;
- an old-generation in-flight delivery is rejected as `peer_rotated`;
- recovery uses `RECOVERY_PREFILL` through the replacement Router and sidecar;
- token indexes remain contiguous and unique until the expected token count;
- pending deliveries return to zero.

## Negative and rejected-evidence harness

`tests/qualification_adversarial/test_physical_negative_runs.py` executes the required ten forbidden mutations through the sole qualifier:

- stale proof;
- wrong revision;
- wrong endpoint;
- missing tensor;
- expired reservation;
- sequence replay;
- dropped peer;
- full-model fallback;
- simulator participation;
- synthetic timing.

Each generated record derives its reason from the actual caught `QualificationError`, binds `evidence_digest` to the exact mutated evidence manifest, and fixes `route_ready=false`. Each rejected tree is also sealed create-new and re-read through `qualify_sealed_evidence`; qualification still fails with the same stable code and the immutable rejected tree remains present. An explicitly `synthetic_test_fixture` tree is rejected by the physical sealer before output creation.

These are harness proofs, not the required physical negative runs under `.myc-phys/<run-id>/negative/`.

## Executed verification

Docker integration environment:

- Hardware-independent Python suite: `1336 passed, 7 skipped, 3 deselected`.
- Bounded production conformance: `15 passed`, including all 4,385 lifecycle traces.
- Native request/Iroh E2E: `6 passed`.
- Negative/rejected sealing harness: `23 passed`.
- Ruff: passed.
- Python compilation: passed.
- `git diff --check`: passed.
- Contract audit: 14 contracts, passed.
- Claim-boundary audit: no findings; `route_ready=false`, `release_ready=false`.
- Release-security audit: no findings; `route_ready=false`, `release_ready=false`.
- Rust: format and clippy passed; 23 tests passed.
- Web UI `npm run check`: passed, including 289 Vitest tests, contract tests, operator-contract tests, typecheck, and production build.
- `npm audit`: zero vulnerabilities.

Environment-only exclusions from the broad Docker suite:

- Eleven Apple-MLX collection paths cannot import `mlx` on Linux.
- Two bootstrap CLI tests hardcode unavailable `python3.14`.
- Two node-process tests spawn MLX-dependent physical services.
- One release-security test cannot model an unprivileged `chmod(0)` read failure while tests run as root.

The excluded behaviors require native non-root macOS/Python 3.14 verification; they were not converted into skips or weakened in source.

## Remaining physical blockers

Native exact-patch and multi-host proof remain blocked by connectivity:

- configured M4 SSH alias points to stale Tailscale address `100.84.252.4` and times out;
- M4 LAN `192.168.0.52` times out;
- coordinator Mac `192.168.0.48` times out;
- Astra tailnet `100.117.33.124` times out.

Before physical promotion:

1. Restore the M4 connection and run the exact patch under native Python 3.14 + MLX.
2. Restore a second eligible Mac and execute the real physical route, remote physical-node death, replacement, idle-heartbeat scenario, and all ten physical negative runs.
3. Stop all writers, seal immutable physical bytes once, invoke the sole qualifier with real signature verifiers, and preserve rejection without weakening gates if any requirement fails.
4. Keep `route_ready=false` unless that sealed physical qualification returns an accepted record.
