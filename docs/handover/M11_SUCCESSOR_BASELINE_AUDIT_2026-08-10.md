# M11 successor baseline audit — 2026-08-10

## Outcome

Task 0 of the post-M11 architecture plan is sealed. M12 may begin with one explicit
prerequisite limitation: the live route still refreshes assignment offers with a
process-local seed signer. Durable seed identity is specified as M12.0 and is not
misrepresented as completed M11 behavior.

## Checklists and physical baseline

M7, M8, M9, and M10 checklist items are complete. M11 is complete except for durable
seed identity, which the successor plan deliberately moves into M12.0. The private
physical sources prove:

- M7: three-host topology including Surface participation;
- M8: two-host MLX route with stage-local KV telemetry;
- M9: larger Qwen 1.5B deployment and its recorded availability limitation;
- M10: two sealed deployment records and immutable deployment selection; and
- M11: Pixel 8 Device Lab participation, live route projection, restart/reconnect,
  recovery, and fail-closed behavior.

The larger-model route must be freshly rebuilt and qualified after its recorded decode
timeout. Pixel Device Lab evidence is not an activation-eligible model stage. Fatal
route recovery still rebuilds and requalifies a complete topology; it does not migrate
in-flight KV state.

## UI acceptance

Direct full-load checks passed for Inference, Device Lab, Network, Nodes, Plans,
Readiness, Incidents, and Settings in both live and fixture modes: 16 route checks.
Live mode showed current physical evidence and authoritative qualification. Fixture
mode showed `FIXTURE DATA · NOT LIVE`, disabled inference/mutations, and synthetic or
modelled provenance. Existing UI tests cover section switching and refresh persistence.

## Frozen compatibility surface

`contracts/contract-manifest.v1.json` now pins 23 unique executable protocols and their
owning sources. Added successor fixtures cover the signed membership envelope,
execution graph, path manifest, live-route status, Router wire, layer-replan simulation,
product bootstrap, and semantic Observatory snapshot/event. Live-route status contains
bounded runtime/KV summary fields; it is not a standalone runtime/KV contract. The
layer-replan fixture is simulation evidence, not a physical recovery report. Generation
and manifest checks are byte-deterministic. Contract audit result: `checked=23`,
`drift=[]`, `ok=true`.

The contract claim boundary remains compatibility only, not current physical
qualification. The specification is
`docs/contracts/M11_SUCCESSOR_BASELINE_FIXTURES.md`.

## Verification record

- Python canonical runtime: `python3.14 -m pytest -q`
  - `3506 passed, 12 skipped, 121 subtests passed`
  - two focused post-collection sealer privacy tests also passed
- TypeScript/UI: 52 files, 358 tests; contract scripts, typecheck, and production build
  passed
- Rust: 33 tests passed (19 library, 2 capability, 9 wire golden, 3 security)
- Ruff: changed Python files clean
- Claim-boundary audit: 309 source files, passed
- Release-security audit: 783 tracked files, passed
- Contract fixture and manifest drift checks: passed
- Production build warning retained for M21: several chunks exceed 500 kB; this is not
  hidden as a correctness failure

The lightweight `.venv` is intentionally contract-only and lacks MLX/NumPy. A full
suite invocation there stopped during collection. The canonical documented runtime is
`python3.14`; it contains MLX/NumPy and produced the green result above.

## Sealed owner-only bundle

Bundle:
`/Users/evinova-self/mycelium-physical-run/m11-successor-baseline-20260810T081655Z`

- directory mode: `0700`
- `evidence.json` mode: `0600`, SHA-256
  `6926169ea38ffda70b667caf8b42b3e5fa851906afd3e130ae2473868d0617c2`
- `manifest.json` mode: `0600`, SHA-256
  `d51ac409fdd43c6409e854743e4f0b301fc208c622389afa3715c33da0b186ee`

The manifest pins six private source artifacts by role, digest, and size without
publishing their paths or raw bytes. The bundle excludes TLS private keys/certificates,
membership databases, operator plans, SSH identities, prompts, outputs, token IDs,
activations, KV contents, and larger-model quality-gate content. Observatory evidence
contains no prompt or output. Inference content remains only in its existing owner-only
Inference-policy artifacts and is not bundled.

The reproducible sealer is `scripts/seal_m11_successor_baseline.py`; adversarial tests
prove it drops private host/path fields and prompt/output/token fields and rejects any
denied field that reaches the final projection.

## M12 entry

The focused specifications are:

- `docs/superpowers/specs/2026-08-10-mycelium-m12-durable-membership.md`; and
- `docs/superpowers/specs/2026-08-10-mycelium-m12-evidence-spine.md`.

The first implementation slice replaces signer generation inside live membership
refresh with one explicitly supplied owner-only durable signer, then projects its
public digest/generation through the privacy-reduced product snapshot and UI.
