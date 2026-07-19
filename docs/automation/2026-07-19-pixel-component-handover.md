# Pixel component completion handover

Status snapshot: 2026-07-19 16:14 CEST
Repository worktree: `/Users/evinova-self/Projects/mycelium-wt-distributed-proof`
Branch: `integration/mycelium-distributed-proof-20260719`
Pixel implementation commit: `40f3941` (`feat(mobile): prove authenticated Pixel stage execution`)
Canonical main remains: `f2bc55b62c5e3103eda584c1c68c222244bde489`
Final immutable evidence: `/Users/evinova-self/.mycelium/evidence/pixel8-final-20260719T140813Z`

## 1. Completion decision

The bounded Pixel component is complete for its stated local physical-evidence scope.

Observed implementation and evidence now cover:

- a dependency-free Python Pixel worker suitable for Termux;
- an authenticated and bounded HTTP client/server protocol;
- a canonical digest-bound derived decoder-substage pack;
- exact parent assignment, parent load-proof, worker-source, tensor, and supported model-configuration binding;
- replay fencing, request identity checks, input bounds, connection bounds, deadlines, strict JSON parsing, and fail-closed errors;
- independent local MLX entry and final subprocesses surrounding real Pixel execution;
- exact hidden-state and final-logit parity against an independent MLX reference;
- exact round-to-even quantized token parity;
- exact HTTP 401 authentication rejection evidence;
- exact HTTP 400 assignment-mismatch rejection evidence with no request-count advance;
- two complete lifecycles with distinct Pixel PIDs and runtime instance IDs;
- explicit shutdown response, old-PID death, endpoint unreachability, restart, and final cleanup;
- immutable per-document JSON evidence with SHA-256 checksums and mode 0600.

This completion does not promote the route. `route_ready=false` remains mandatory.

## 2. Delivered files

- `mycelium_mobile/__init__.py`
- `mycelium_mobile/pixel_client.py`
- `mycelium_mobile/pixel_stage.py`
- `mycelium_mobile/termux_bridge.py`
- `physical_pixel_host_stage.py`
- `tests/mobile/test_pixel_stage.py`
- `tests/physical_qualification/test_pixel8_distributed_inference.py`
- `README.md`

The Observatory was not changed and remains read-only.

## 3. Final live physical result

Command lane: `tests/physical_qualification/test_pixel8_distributed_inference.py` with the explicitly authorized Pixel 8 Pro bridge/worker endpoints and evidence directory.

Observed result:

- `2 passed in 15.92s`
- Pixel: `Pixel 8 Pro`, Android SDK 36, `arm64-v8a` / `aarch64`
- lifecycle process IDs: `9340`, `9416`
- lifecycle runtime IDs: `d5259e26-2e56-4cf4-8b5a-e0f147aa337b`, `07b0ab7f-e22c-4924-9e76-8a4fa9eb29d1`
- maximum intermediate error: `4.0139550794293655e-08`
- maximum final-logit error: `4.880130290985107e-07`
- quantized actual tokens: `[[0,0,0,0],[0,0,0,0]]`
- quantized expected tokens: `[[0,0,0,0],[0,0,0,0]]`
- bad authentication: exact HTTP `401`, error `unauthorized`, twice
- mismatched assignment: exact HTTP `400`, error `request_assignment_mismatch`, twice
- both old PIDs observed dead
- both old endpoints observed unreachable with `pixel_transport_failed`
- restart confirmed by distinct process/runtime identities
- final worker endpoint observed closed

Evidence verification:

- all 11 JSON files passed `shasum -a 256 -c SHA256SUMS`;
- `SHA256SUMS` plus 11 JSON documents gives 12 files total;
- all evidence files are mode 0600;
- no secret token is archived.

Authoritative evidence files include `summary.json`, `lifecycle-events.json`, the derived stage pack, parent assignment/load proof, both health documents, both worker evidence documents, and both verification documents.

## 4. Independent review record

Independent semantic/security review found and drove repair of these claim or correctness gaps before completion:

1. Bound the exact supported GPT-2 attention/activation configuration into the stage pack and reject unsupported configurations.
2. Narrowed hardware wording from an unproven M4 identity to observed local MLX subprocesses.
3. Narrowed shutdown wording from port closure to observed endpoint unreachability.
4. Added structured lifecycle evidence and derived summary booleans from observed events.
5. Replaced ambiguous client-level bad-auth evidence with exact HTTP `401` plus `unauthorized` assertions and archive fields.

Final blocker-only review reported no other high/medium finding after identifying item 5; item 5 was repaired and physically rerun.

## 5. Final gates

All required gates were rerun after the final authentication-evidence repair.

### Python and contracts

- `MYCELIUM_PIXEL8_LIVE=0 python3.14 -m pytest -q`
  - `1699 passed`
  - `3 skipped`
  - `121 subtests passed`
  - exit 0
- `python3.14 scripts/contract_audit.py`
  - `contract audit OK: 14 contracts`
  - exit 0
- `python3.14 -m compileall -q .`
  - exit 0
- `git diff --check`
  - exit 0
- `git diff --cached --check`
  - exit 0

### Python quality and claim/security audits

- targeted `ruff format --check`: 7 files already formatted, exit 0
- targeted `ruff check`: all checks passed, exit 0
- `python3.14 scripts/claim_boundary_audit.py`
  - `claim boundary audit OK: 186 source files`
  - exit 0
- `python3.14 scripts/release_security_audit.py`
  - `release security audit OK: 492 tracked files`
  - exit 0

### Native iroh transport

From `native/iroh_transport`:

- `cargo fmt --check`: exit 0
- `cargo clippy --all-targets --all-features -- -D warnings`: exit 0
- `cargo test`: 21 passed, 0 failed, exit 0

### UI

The lane lockfile and `package.json` are byte-identical to canonical main. This worktree had no local `node_modules`; verification temporarily symlinked canonical main's existing `ui/web/node_modules`, ran the gate, and removed the symlink. No package was installed and canonical files were not changed.

`npm run check`:

- Vitest: 9 files, 98 tests passed
- Node contract tests: 3 passed
- TypeScript typecheck: passed
- production Vite build: passed
- exit 0
- non-blocking warning: some generated chunks exceed 500 kB

## 6. Reconciliation provenance

The branch ancestry preserves the requested overnight automation journal and integration work:

- retained `e9e6928` as rewritten integration commit `13d9a387` (`Add isolated overnight build queue`);
- retained `cd7d1b1` as rewritten integration commit `5d6c2e12` (`Coordinate overnight session continuation`);
- skipped `ead89f2` because its A1 two-process qualification implementation already existed byte-identically on canonical main at `f2bc55b`;
- integrated `b759305` through `d412777` in source order as rewritten commits `3b457a77` through `95c79ce5`, including the two journal-only provenance tranches;
- repaired clean-checkout documentation authority in `9f2cc35b` and recorded reconciliation in `708a7100`;
- subsequent feature, hardening, physical-evidence, and distributed-inference commits are preserved in branch ancestry through `17c95fac`;
- Pixel completion is commit `40f3941`.

No merge to main and no push occurred.

## 7. Exact claim boundary and remaining gaps

Strongest honest Pixel claim:

> A local MLX subprocess sent assignment-derived hidden states through an authenticated HTTP derived decoder substage executing on a real Pixel 8 Pro Termux process, then a second local MLX subprocess completed the deterministic tiny-GPT-2-shaped fixture. Two lifecycles matched an independent local MLX reference within `1e-6`, matched explicitly quantized token selection exactly, rejected bad authentication and assignment identity, and proved worker death/restart/cleanup.

Not proved and not claimed:

- production Router transport to the Pixel;
- production request-gateway or UI-triggered Pixel inference;
- production route qualification or device authority;
- live ADB identity during the final run (bridge and worker were instead bound by the Android boot ID);
- cold-cache peer-direct model artifact acquisition;
- a pretrained or user-selected model;
- stage-local KV continuity across a production routed decode;
- multi-host native-iroh Pixel transport;
- elimination of the generic bootstrap bridge, whose credential is RCE-equivalent;
- browser-level visualization of this physical Pixel lifecycle.

The fixture is deterministic and locally generated. The phone executes a derived decoder substage, not a full model or a production assignment as its own route-ready authority.

## 8. Next lane: UI readiness double-check

Before asking a user to test through the UI, verify rather than assume:

1. identify the UI's current runnable start command and existing dependency source;
2. identify which real observatory/request endpoints the UI consumes;
3. confirm whether Pixel lifecycle events are projected into the read-only Observatory at all;
4. confirm whether any authenticated request submission surface exists outside the Observatory;
5. start the backend and UI from this exact integration branch without installs;
6. exercise the browser against real local endpoints and inspect console/network failures;
7. preserve the read-only Observatory boundary;
8. do not describe UI display as Pixel inference unless the displayed data is actually sourced from the Pixel lifecycle evidence or a live accepted adapter.

Expected current gap: the Pixel component is physically proven, but direct UI-triggered Pixel inference and Pixel-specific Observatory projection have not been established by this tranche.
