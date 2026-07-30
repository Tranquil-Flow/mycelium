# Mycelium next sessions and device preparation — 2026-07-23

## Verified snapshot

Snapshot time: 2026-07-23T16:57:14+02:00 on unrestricted coordinator macOS.

Completed:
- A8 / Task 3.2 device-free controller: `c3dd159` then `f04e4381bd7c10956f5276907b4b5801dad95ba2`.
- A8 full gate: 2055 passed, 3 skipped, 121 subtests; Rust fmt/test/clippy/build green.
- Independent A8 reviews: specification PASS; security/quality APPROVED; no blocking findings.
- A8 is device-free integration-ready, not physically or release ready.

Still open and verified untouched at base `b317566`:
- A3 / Task 3B.5 optional MLX + assignment-local NumPy stage runtime.
- A4 / Task 2.4 SwarmCoordinator EXTEND.
- A5 / Task 3B.1 planner-placement plumbing.
- A6 / Task 3B.4 request-gateway local qualified-route seam.
- corrective Task 2R.6 / 3.1 exact stage-pack ownership and parity.
- A9 / Task 3B.3 offline directed-cycle study.

Still open with preserved work:
- A7 / Task 2.3B localhost E2E in `/Users/evinova/Projects/mycelium-sess-a7-e2e-v2`; expected dirty file is `tests/seed_coordinator/test_local_two_node.py` on `ab668bac`.

Full prompts for A1–A7/A9 remain in:
`/Users/evinova/Projects/.mycelium-plan/UNRESTRICTED_SESSION_PROMPTS_2026-07-23.md`

Do not relaunch A8 or its two reviews. Sessions 10–11 in that pack are complete.

Device state at 2026-07-23T16:57:14+02:00:
- Astra: exact identity `Astra Macbook` / `astra-macbook.tail53d0d3.ts.net` / `100.117.33.124`; Tailscale `Online=false`; ping and TCP/22 timed out; exact macOS username still unknown. Reject stale `100.78.72.79`.
- Pixel 8 Pro: exact identity `100.126.233.4`, hardware serial `47031FDJG000RD`; Tailscale `Online=true`, ping works; ADB device list empty because Wireless Debugging is not exposing an endpoint.

## Human prerequisites before device sessions

Astra owner must:
1. Explicitly approve the intended inventory/provisioning/test scope.
2. Send the exact output of `whoami` from Astra.
3. Keep the Mac awake and connected to power.
4. Connect Tailscale.
5. Enable System Settings → General → Sharing → Remote Login, or configure Tailscale SSH.

Pixel owner must:
1. Enable Settings → System → Developer options → Wireless debugging.
2. Keep the screen unlocked during initial reconnection.
3. If existing mDNS discovery does not reconnect, provide the displayed connect endpoint. Do not assume port 5555.

---

## Prompt 1 — next critical-path code session: A3 / Task 3B.5

Launch now on unrestricted coordinator macOS. This is the highest-priority single new code session because it unlocks a real non-MLX assignment-local runtime path needed before any honest Pixel G9 work.

```text
You own Mycelium A3 / Task 3B.5 runtime completion.

Run only on unrestricted host macOS, not Linux Docker and not an Aegis seatbelt. Work only in:
/Users/evinova/Projects/mycelium-sess-a3-runtime
Branch: feat/session-3b5-runtime-completion
Required starting HEAD: b317566a73d8c7aebafc76bcbcca7f8eb651163e

Before editing, verify the worktree is clean, python3.14 exists, native socket tests are unrestricted, and these private plans exist:
- /Users/evinova/Projects/.mycelium-plan/MVP_COMPLETION_PLAN.md
- /Users/evinova/Projects/.mycelium-plan/mycelium-demo-plan.md
Read Task 3B.5 and §6.13 in full.

A8 is complete elsewhere at c3dd159 + f04e438. Do not modify or copy its controller/Rust files. Do not touch physical hardware.

Run the shared baseline gate before editing:
python3.14 /Users/evinova/Projects/.mycelium-plan/run_full_suite_locked.py --repo /Users/evinova/Projects/mycelium-sess-a3-runtime
Require the clean b317566 baseline. If the sole failure is the known intermittent exact-cap-frame test, run that exact test three isolated times and report all outcomes; do not change its timeout. Final full suite must have zero failures.

Observed focused baseline at b317566:
python3.14 -m pytest -q -p no:cacheprovider test_numpy_runtime.py test_runtime_loader.py test_router_mlx_runtime.py
Expected existing baseline: 72 passed.

Objective:
1. Make MLX optional and lazily imported. Importing runtime contracts, NumPy runtime, and backend selection must work when MLX is absent.
2. Add a real assignment-local NumPy stage backend. It must consume an already authenticated assignment/stage-pack artifact, load only assignment-owned tensors, execute entry/intermediate/final stage roles, and preserve route_ready=false absent a qualified route.
3. Prove stage output parity against MLX within the already-frozen tolerance. Do not modify the tolerance.
4. Preserve current MLX behavior exactly.

Exclusive owned files:
- runtime_contracts.py
- runtime_loader.py
- numpy_runtime.py
- test_numpy_runtime.py
- test_runtime_loader.py
- one new narrowly named NumPy-stage test file if needed

Do not modify:
- stage_pack.py or test_stage_pack.py
- mycelium_seed/**
- mycelium_interactive/**
- mycelium_request_gateway/**
- physical_inference_qualification.py
- native/iroh_transport/**
- signed membership/evidence contract shapes

Required strict TDD RED cases:
- With monkeypatch.setitem(sys.modules, "mlx", None), runtime selection imports and selects a real NumPy backend. Never ship a fake mlx module.
- Explicit MLX selection while absent fails with a stable backend-unavailable error, not an import traceback.
- NumPy stage loads only exact assignment tensor keys.
- Entry, intermediate, and final roles execute; unsupported role/pack combinations fail closed.
- Same stage input produces NumPy hidden states/logits within frozen tolerance of MLX and exact greedy argmax/token result.
- Invalid dtype, shape, nonfinite tensor, assignment mismatch, and unverified pack fail before execution.
- Fallback execution never claims route readiness or physical proof.

For each behavior: write RED test, run and observe intended failure, implement minimum production change, rerun focused tests. Then run compile and Ruff on owned files and the locked complete suite. Never add ignores/skips/xfails or alter tolerance/timeouts.

Stage exact owned paths only; never git add -A or git add . Commit locally with plain subject:
feat: execute assignment-local stages with numpy
Do not push or merge.

Report commit SHA, exact focused/full counts, MLX-absent proof, parity values, changed files, and what production Router/Iroh work still blocks Pixel G9. Do not call NumPy membership or HTTP staging production heterogeneous execution.
```

---

## Prompt 2 — Astra access and read-only readiness

Launch only after the human prerequisites above are satisfied and replace `<ASTRA_USERNAME>` with the exact `whoami` output.

```text
You are the Astra read-only Mycelium readiness session.

Run on the unrestricted Evi coordinator Mac. No Linux Docker, Aegis seatbelt, or guessed credentials. Owner consent is required and has priority over coordinator convenience.

Target authority:
- Machine: Astra Macbook
- MagicDNS: astra-macbook.tail53d0d3.ts.net
- Current Tailscale IP candidate: 100.117.33.124
- Exact macOS username: <ASTRA_USERNAME>
- Stale forbidden IP: 100.78.72.79

If `<ASTRA_USERNAME>` was not replaced, owner consent is not explicit, or Astra is offline, stop without trying usernames or changing anything. Return the one missing gate.

Use `/opt/homebrew/bin/tailscale status --json` on the coordinator and bind the peer identity before any ping/SSH. Reject stale addresses. Run bounded ping and TCP/22 only after identity match.

Preserve host-key security. Use StrictHostKeyChecking=yes and BatchMode=yes. If the host key is unknown, collect only the offered ED25519 fingerprint and stop so the operator can compare it with Astra locally. Never auto-accept, disable changed-key protection, edit known_hosts, or create an SSH alias during this inventory.

After authenticated SSH succeeds, collect only:
- whoami, hostname, macOS version, architecture, boot identity, local time;
- Tailscale direct/relay path class;
- memory and disk headroom;
- Python 3.14 presence/version;
- MLX import/version and a tiny existing-device operation only if already installed;
- Rust/cargo and native Iroh tooling presence/version;
- existing Mycelium checkout paths, exact HEAD/branch/status without fetch/reset;
- pinned model presence, dereferenced byte size, revision/digest without printing tokens or cache internals;
- native sidecar architecture, digest/version, and a bounded local health check only if already built;
- scoped Mycelium processes/ports only, not unrelated applications/files.

Do not install, clone, download, build, configure, wake services, modify source, create evidence, launch a peer, or run physical inference. Missing state is a provisioning blocker, not permission to fix it.

Write no file on Astra. Return a concise readiness matrix with true/false/unknown, exact blockers, and:
route_ready=false
release_ready=false
physical_evidence_written=false
Strongest claim is read-only inventory only.
```

---

## Prompt 3 — Astra provisioning after approved inventory

Launch only after Prompt 2 returns, Astra explicitly authorizes provisioning, and missing prerequisites are known.

```text
You are the Astra Mycelium provisioning session. Provision prerequisites only; do not run qualification.

Prerequisites:
- Astra owner explicitly approved the exact install/sync scope.
- Read-only inventory report from the prior session is available.
- Exact target identity and username are verified.
- SSH host key has been physically compared and pinned normally.

Use the prior inventory as evidence, but revalidate identity and SSH before changing anything. Make a written change plan listing each install/download/file transfer and obtain approval if scope exceeds the inventory blockers.

Provision only what the canonical Mycelium plan requires for the two-Mac FP16 path:
- compatible Python 3.14 environment;
- MLX and exact required Python packages in a project-specific venv;
- Rust/cargo/native Iroh build prerequisites;
- Tailscale already configured by Astra;
- pinned model from `/Users/evinova/Projects/.mycelium-plan/mycelium-demo-plan.md`, with exact revision and digest.

Do not copy GitHub, Hugging Face, SSH, API, or gateway credentials to Astra. Prefer transferring already-pinned public model bytes from the coordinator after computing a manifest, or perform a public download before qualification if explicitly approved. Verify dereferenced size and SHA-256 on both machines.

The final source commit does not exist until all producer lanes are integrated. Before integration, provision toolchains/model only and report source sync deferred. After the serialized integration driver produces a clean exact commit, transfer source without secrets using a git bundle or other manifest-verifiable method, check out that exact commit under an Astra-owned project path, build the native sidecar on Astra, and verify source/model/sidecar digests against the coordinator.

Never use the qualification evidence directory for provisioning logs. Never download/build during a qualification run. Do not start persistent peers or create route-ready records.

Run bounded smoke checks only:
- Python/MLX import and tiny tensor operation;
- native sidecar version/help and local-only health with complete cleanup;
- exact source/model/sidecar digest parity after final integration sync.

Remove temporary archives and installers you created, but do not delete pre-existing files. Return an explicit change log, versions, digests, cleanup proof, remaining blockers, and:
route_ready=false
release_ready=false
physical_evidence_written=false
```

---

## Prompt 4 — Pixel authenticated readiness and reversible Termux preparation

Launch after Wireless Debugging is enabled and the Pixel is unlocked.

```text
You are the Pixel 8 Pro Mycelium readiness session. Run from the unrestricted Evi coordinator Mac.

Target identity:
- Tailscale: Pixel 8 Pro / 100.126.233.4
- Hardware serial expected inside authenticated shell: 47031FDJG000RD
- Existing ADB binary: /Users/evinova/Library/Android/sdk/platform-tools/adb
- Existing ADB server port: 5037

Use existing ADB authorization only. Start with:
- identity-bound Tailscale status/ping;
- `adb -P 5037 devices -l`;
- `adb -P 5037 mdns services`.

Do not assume port 5555. If mDNS does not reconnect, ask for the displayed Wireless Debugging connect endpoint. A direct connect using an already-authorized key is allowed; a new pairing, APK install, bootloader action, root action, VPN change, or developer-setting change is not allowed without separate explicit approval.

If exactly one authorized device appears, verify minimal identity before any other action:
- manufacturer/model/device;
- Android release/API/ABI;
- build fingerprint and boot ID;
- `ro.serialno` or `ro.boot.serialno` equals 47031FDJG000RD;
- Tailscale package and Termux package presence by exact package name only.
Do not dump broad package, process, log, contacts, media, notification, or account inventories.

If Termux is absent, stop. Do not install an APK. If present, inspect whether an existing authenticated argv-only bridge exists. Never print bridge tokens. Prove unauthenticated rejection and authenticated identity only if the bridge already exists.

If Termux RUN_COMMAND is required, you are authorized to make a bounded reversible toggle only after:
1. backing up the exact existing Termux properties file;
2. recording its digest/mode;
3. setting allow-external-apps=true only for the qualification step;
4. using argv-only commands with no shell interpolation of secrets;
5. restoring the exact original bytes/mode before exit;
6. deleting every ephemeral bridge/token/helper created in this session;
7. verifying no helper remains.
Do not leave allow-external-apps enabled.

Before final integration, inventory only. After A3 and the serialized integration commit exist, determine which exact Pixel role is actually implemented:
- membership/HTTP host-stage path through physical_pixel_host_stage.py is partial evidence only;
- assignment-local NumPy runtime alone is not production Router transport;
- G9 requires a real assigned layer executed through production Router/Iroh activation transport on the Pixel.
If no Android production Iroh worker exists, report `G9 blocker: production Android Router/Iroh transport absent` and do not simulate success.

Run no model download, no long-lived worker, no sealed evidence, and no physical claim in this readiness session. Return exact strongest rung, reversible-change proof, blockers, and:
route_ready=false
release_ready=false
physical_evidence_written=false
```

---

## Prompt 5 — serialized integration driver

Save now; launch only when every producer session has committed, returned a clean handoff, and passed review.

```text
You are the sole Mycelium integration writer. Do not begin integration until A3, A4, A5, A6, corrective 2R.6, A7, and A9 are all committed, clean, and independently reviewed. If any is dirty/unfinished, return a dependency table and stop without changing integration.

Run on unrestricted coordinator macOS. Create a fresh clean worktree from the latest integration/mycelium-multi-device-readiness. Re-run the read-only branch reconciliation and verify each commit by content, not subject.

Apply only reviewed commits in dependency-aware order:
1. ab668bacd9866e19ca14b7b98a07db8bba756b64 — entrypoint closure;
2. corrective 2R.6 exact pack ownership/parity commit;
3. A3 optional MLX + assignment-local NumPy runtime commit;
4. A5 planner-placement plumbing commit;
5. A6 qualified-route request-gateway seam commit;
6. A4 SwarmCoordinator EXTEND commit;
7. A9 offline directed-cycle tests/docs commit;
8. A7 localhost E2E commit;
9. A8 prerequisite c3dd159;
10. A8 controller f04e4381bd7c10956f5276907b4b5801dad95ba2.

Never cherry-pick f04e438 alone: c3dd159 is its separate bare-preflight prerequisite.

After every cherry-pick, run the shared locked full suite and the lane's focused gate. Stop on first regression. Resolve conflicts semantically; never wholesale choose ours/theirs. Do not modify tests/tolerances/timeouts to force green.

Final gates:
- complete unrestricted Python suite with no exclusions;
- contract and claim-boundary audits;
- release-security/static changed-line audit;
- compile and Ruff, baseline-aware only for unchanged pre-existing findings;
- Rust fmt/test/clippy/build because A8 changes Rust;
- git diff --check and clean status;
- no private plan, token, model cache, evidence, or device-identity artifact staged.

Commit only explicit reviewed paths. Push the Evi-owned integration branch after all gates pass. Do not push shared/third-party repositories. Report final integration SHA, every included source SHA, exact gates/counts, and remaining physical blockers. Do not run devices from this session.
```

---

## Prompt 6 — single-writer multi-device kickoff and qualification

Save now; launch only after Prompt 5 produces a clean integrated commit and Prompts 2–4 report devices ready.

```text
You are the sole Mycelium physical-test driver. One session owns every run ID, peer process, evidence root, cleanup, seal, and qualifier invocation. No other physical writer may run concurrently.

Hard prerequisites before any launch:
- final integrated commit is exact, clean, pushed, and complete-suite green;
- A8 c3dd159 + f04e438 and all producer dependencies are integrated;
- Evi Mac and Astra Mac each have explicit owner consent, distinct host/boot identities, identical source/model/sidecar digests, compatible Python/MLX, and sufficient resources;
- Astra SSH identity/user/host key are verified normally;
- no conflicting scoped Mycelium process/port/UDS exists;
- no provisioning, download, build, or source edit is needed during the run;
- Pixel ADB/Termux readiness is recorded separately.
If any gate is false or unknown, stop before process launch and report it.

Read the final canonical plan's G4/G5/G6/G4b/G8/G9 requirements and use its exact commands/contracts. Re-run the exact documented bare preflight command byte-for-byte before physical work.

Phase 1 — two-Mac headline route:
- Use Evi Mac + Astra Mac only. Pixel must not substitute for G4.
- Run frozen-placement FP16 G4 first with native Iroh.
- Bind signed current-epoch membership, exact endpoint identities, source/model/sidecar digests, stage-local tensor ownership, host/boot/PID/process identity, transport direct/relay class, per-direction link_state, timing, exact tokens, and frozen numeric tolerance.
- Capture negative runs honestly.

Phase 2 — lifecycle:
- Run bounded cancellation and restart/recovery under G5.
- Reject stale generation and prove cleanup across every peer.

Phase 3 — evidence:
- One run, one writer, one immutable evidence tree, one seal, exactly one sole qualifier invocation.
- Stop all writers before sealing.
- Never edit evidence, golden outputs, tolerance, or timeout to make a run pass.
- route_ready may become true only if the sole qualifier accepts immutable same-run evidence. release_ready remains false unless a separate release gate says otherwise.

Phase 4 — later separate run IDs:
- G4b planner-placement A/B is a separate pair of physical runs after A5 integration; require materially different measured capacity and changed assignments with exact token parity.
- Pixel membership/HTTP host-stage participation is partial evidence only and must be labeled partial. It cannot prove an activation-carrying third production peer.
- Do not claim G8 swarm execution until a third physical peer receives a capacity-derived assignment through the required production path.
- Do not claim G9 heterogeneous execution until the Pixel executes a real assigned layer through production Router/Iroh activation transport. NumPy runtime or HTTP staging alone is insufficient.

On any source change, dependency drift, digest mismatch, device sleep, cleanup failure, or null result: preserve the result, stop, fix under TDD in a separate source session, and restart with a new run ID. Never patch source inside evidence.

Final report must separate every proof rung: host-green, two-device transport, G4, G5, G6, G4b, partial Pixel evidence, G8, G9. Include exact cleanup evidence and make no claim above the highest accepted rung.
```

## Launch order

Start now:
1. Prompt 1 (A3).
2. Existing Session 1 (A1 read-only branch reconciliation) and Session 2 (A2 private-plan status reconciliation) from `UNRESTRICTED_SESSION_PROMPTS_2026-07-23.md`.
3. Existing code prompts Sessions 4–9 from `UNRESTRICTED_SESSION_PROMPTS_2026-07-23.md` in parallel, respecting their exclusive file surfaces and the shared full-suite lock.
4. Prompt 2 only after Astra owner prerequisites are satisfied.
5. Prompt 4 only after Pixel Wireless Debugging is enabled.

Then:
6. Prompt 3 after Astra inventory and explicit provisioning approval.
7. Prompt 5 after every producer is committed, clean, and reviewed.
8. Prompt 6 after integration and both device-readiness reports are green.

Claim boundary: Pixel is currently useful for authenticated membership/HTTP-stage readiness work, but it is not yet a production Router/Iroh activation-carrying peer. Evi + Astra are the honest first G4 pair.
