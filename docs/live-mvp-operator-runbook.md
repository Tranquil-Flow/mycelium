# Mycelium live MVP operator runbook (M22)

This runbook operates the bounded M7–M22 product: one or more sealed physical
pipeline deployments, capability-aware contiguous allocation, measured directed
cycle selection, qualified multi-model selection, continuous membership renewal,
durable service packaging, truthful failure handling, and the isolated Device Lab
browser-worker path.

## Claim boundary

- A deployment is live only after every planned native process starts, the physical
  startup challenge reproduces its expected tokens, and the qualifier accepts the
  resulting evidence.
- Browser workers perform only the labelled deterministic matrix fixture. They are
  never model stages and never make `route_ready=true`.
- The current live route is pipeline parallel across explicit layer ranges. M14 can
  select an exact measured three-host cycle and then run the contiguous allocation
  DP in that order. It does not claim tensor parallelism, data-parallel replicas,
  microbatch overlap, continuous topology re-optimization, or automatic in-flight
  KV migration.
- Homogeneous MLX routes may qualify `stage_local_kv`. A route containing a NumPy
  stage uses `complete_context_replay`; never present that as cross-backend KV.
- The `int8-weight-only` deployments quantize weights when each runtime loads its
  stage. The transferred Safetensors stage packs remain in their source format.

## 1. Preflight and inventory

Use the exact Python interpreter expected by the repository:

```sh
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=.
/opt/homebrew/bin/python3.14 -m mycelium_demo doctor \
  --state-dir /absolute/private/state/root \
  --port 8791 --port 8792
```

Every native peer must be reachable by the transport and by the controller command
recorded in the operator plan. A shared Tailscale network is not a protocol
requirement, but it is the current authenticated reachability layer for hosts that
are not on the same LAN. An offline peer is unavailable; never replace it with a
local or simulated process.

Before building a route, record free RAM, free disk, backend/runtime version, stable
host/boot identity, and route RTT for every candidate. Leave activation, KV, and OS
headroom when selecting layer ranges.

### MacBook worker availability

An ordinary `caffeinate` process prevents idle sleep while the MacBook is open, but
does not reliably override sleep caused by closing the lid. For a supported closed-lid
worker, connect AC power, an external display, and an external keyboard or mouse, then
verify the SSH and transport paths after closing the lid. If the operator explicitly
accepts the thermal and battery risk of an unsupported lid-closed setup, an administrator
may apply the reversible system setting below while the computer is open and powered:

```sh
sudo pmset -a disablesleep 1
# Restore normal lid-sleep behavior after the worker session:
sudo pmset -a disablesleep 0
```

Never claim a MacBook worker is durable merely because `caffeinate` is running. Before
every proof, close the lid only under the chosen supported or operator-approved mode,
then confirm the host remains reachable, its expected worker PID and boot identity are
unchanged, and a fresh startup challenge succeeds. Treat a sleeping or unreachable host
as unavailable and fail the route closed.

On the current Evi MacBook Pro, open-lid protection is installed as the user LaunchAgent
`~/Library/LaunchAgents/com.mycelium.keep-awake.plist`. Verify it after login with
`launchctl print gui/$(id -u)/com.mycelium.keep-awake`; this is still not evidence that
lid-triggered sleep has been disabled.

## 2. Import once and materialize stage packs

Download or import one pinned Hugging Face snapshot into the operator's immutable
cache. Verify its revision and file digests there. Do not download it independently
on every worker.

Build the route from that one cache entry:

```sh
/opt/homebrew/bin/python3.14 scripts/build_qwen_live_route.py \
  --help
```

Supply the exact model snapshot, ordered peer inventory, layer ranges, staging
roots, runtime backends, decode mode, and output root required by the invocation.
The builder emits:

- a deterministic deployment identity;
- an operator plan and controller inputs;
- a common tokenizer/config bundle;
- one digest-bound Safetensors bundle per stage owner;
- per-node transfer byte totals.

Inspect the build report before staging. Layer ranges must be non-empty, contiguous,
non-overlapping, and cover the entire decoder exactly once.

For large checkpoints, `--stage-sharded` deterministically splits any stage file
that would exceed the controller's 2 GiB per-artifact verification boundary. A
multi-part stage still has one layer owner; each part is independently digest-bound
and the checkpoint index maps every tensor to exactly one part. Never raise the
controller limit to force a large single file through. The current local catalog
includes the dense Qwen3-8B adapter, but admission must remain blocked whenever its
measured memory or disk requirements do not fit the current swarm.

## 3. Stage without launching

```sh
/opt/homebrew/bin/python3.14 scripts/stage_live_route.py \
  --operator-plan /absolute/path/to/operator-plan.json
```

The command must report one physical acknowledgement per node. Remote staging streams
the archive through an owner-only temporary file while calculating the digest; it must
not buffer the complete archive in RAM. For node-specific
bundles, archive digests may differ, but every digest and byte count must match the
controller's node transfer manifest. Staging alone is not readiness evidence.

## 4. Serve one or multiple qualified deployments

Build the UI in live mode:

```sh
cd ui/web
npm run build:live
cd ../..
```

For a single deployment:

```sh
/opt/homebrew/bin/python3.14 -m mycelium_demo serve --mode live \
  --operator-plan /absolute/path/to/operator-plan.json \
  --seed-state-root /absolute/private/mycelium-seed \
  --host 127.0.0.1 --port 8791 \
  --static-root /absolute/path/to/ui/web/dist
```

For model selection across two or more deployments:

```sh
/opt/homebrew/bin/python3.14 -m mycelium_demo serve --mode live \
  --operator-plan /absolute/path/to/baseline/operator-plan.json \
  --operator-plan /absolute/path/to/larger/operator-plan.json \
  --seed-state-root /absolute/private/mycelium-seed \
  --registry-state /absolute/private/state/deployments.json \
  --host 127.0.0.1 --port 8791 \
  --static-root /absolute/path/to/ui/web/dist
```

The supervisor opens and challenges every physical route before binding the product
server. If any challenge or endpoint identity check fails, no live listener is
published. Open `http://127.0.0.1:8791/#inference`; a model switch is accepted only
while no request is active and only to another currently qualified route. Existing
tab-session history keeps the deployment ID and model name captured at admission.

Browser admission treats a qualification older than one hour as stale. The live
supervisor renews at 55 minutes, on the next qualification or Observatory read, by
rerunning the exact signed physical startup challenge while the request router is
idle. It never changes a binding underneath an active request. If renewal fails,
the old record is retained only for retry and the browser continues to fail closed
once its freshness limit is reached; `Refresh` is not a freshness bypass.

## 5. Observe and accept

Exercise at least two unseen prompts. Verify all of the following in the product UI
and public, prompt-free status projection:

- output is streamed from the selected physical model;
- every stage owner has a non-zero counter delta;
- TTFT, TPOT, prefill, total time, and context/output lengths are present;
- all stage-local KV counts return to zero after completion or cancellation;
- Network shows the exact ordered layer graph;
- for M14, Network shows all measured directed edges, selected cycle, explicit
  loopback, path class, pricing inputs, freshness, and connection reuse;
- Nodes shows native participants as qualified/direct;
- Plans labels measurements as observed rather than modeled;
- Readiness shows accepted physical qualification and no fatal state;
- request history survives section switching and a full page reload;
- model switching never relabels an older request.

Run the unseen quality gate against the larger deployment:

```sh
/opt/homebrew/bin/python3.14 scripts/run_live_quality_gate.py \
  --base-url http://127.0.0.1:8791 \
  --output /absolute/private/evidence/quality-gate.json
```

Promotion requires factual, arithmetic, instruction-following, and safe-refusal
results to pass the documented rubric. A failed refusal case remains a failed gate;
do not hide it behind aggregate latency or route success.

## 6. Failure, drain, and rebuild

For an intentional proof, terminate exactly one verified worker PID or its
staging-root-bound process. Do not use an unbounded process pattern. The next status
poll or request must show the route unavailable or failed closed, and new work on
that deployment must be rejected.

If another deployment remains qualified, select it only after the failed request has
reached a terminal state. The live Incidents section records the failed deployment,
public failure reason, affected request when known, and qualified failover selection.
This is deployment failover; it is not continuous KV migration.

Rebuild the failed deployment as a complete topology:

1. Stop new admissions and let any surviving active request reach a terminal state.
2. Send `SIGINT` or `SIGTERM` to the loopback supervisor and wait for every child
   process to exit. Graceful shutdown runs digest-bound cleanup.
3. Confirm no worker process or active KV state remains. If a host was unreachable,
   preserve its staging root until that exact host returns; do not weaken cleanup.
4. Re-run the staging command for every deployment required by the registry.
5. Start the supervisor again. It reissues time-bounded membership offers from the
   current wall clock, starts fresh processes, reruns the startup challenge, and
   republishes readiness only after qualification succeeds.
6. Reconnect or reload the browser. A restarted Observatory generation must advance,
   and terminal tab-session inference history must remain truthful.

## 7. Pixel 8 and browser Device Lab

Choose a LAN or Tailscale address reachable from the Pixel and launch the isolated
HTTPS Device Lab on a different port from physical inference:

```sh
/opt/homebrew/bin/python3.14 -m mycelium_demo device-lab \
  --advertise-host 100.x.y.z \
  --port 8792 \
  --state-root /absolute/private/state/device-lab \
  --static-root /absolute/path/to/ui/web/dist \
  --worker-static-root /absolute/path/to/mycelium_interactive/static
```

Transfer and trust the printed local CA certificate on the Pixel, open the one-time
operator URL on the operator device, create one unique link for the Pixel, and open
that link once. Device Lab must show the Pixel browser session ready before running a
bounded fixture request. Save the local evidence record and verify
`route_ready=false` throughout.

The Android stdlib runtime (currently named `pixel-stdlib` in code for compatibility)
authorizes an experimental mobile activation track. Keep `pixel_http` as the legacy
Device Lab worker class and use the device-vendor-neutral `android_termux_iroh` class
for Router/Iroh stage work. A Pixel 8 is the first conformance device, not a special
protocol target. Do not promote any mobile candidate into a production-qualified route
until it passes exact stage parity plus transport, decode-mode, thermal, battery,
background lifecycle, sleep, and network-loss qualification.

## 8. Test and release gate

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  /opt/homebrew/bin/python3.14 -m pytest -q -p no:cacheprovider
/opt/homebrew/bin/ruff check .
cargo test --manifest-path native/iroh_transport/Cargo.toml
cd ui/web
npm test -- --run
VITE_OBSERVATORY_SOURCE_MODE=live npm run build
cd ../..
/opt/homebrew/bin/python3.14 scripts/contract_audit.py
/opt/homebrew/bin/python3.14 scripts/claim_boundary_audit.py
/opt/homebrew/bin/python3.14 scripts/release_security_audit.py
git diff --check
```

Keep physical quality, counter, timing, failure/failover, browser refresh, Device Lab,
and cleanup evidence outside the source tree in a private run directory. Seal only
the exact approved files and record the resulting canonical manifest digest. Evidence
containing prompts or outputs must never enter Observatory or a public release bundle.

## 9. Durable services and reviewer package

Generate the three service packages from owner-reviewed absolute-path configs:

```sh
/opt/homebrew/bin/python3.14 scripts/package_m22_service.py \
  --config release/service-configs/seed.json --output /private/service/seed
/opt/homebrew/bin/python3.14 scripts/package_m22_service.py \
  --config release/service-configs/node.json --output /private/service/node
/opt/homebrew/bin/python3.14 scripts/package_m22_service.py \
  --config release/service-configs/supervisor.json --output /private/service/supervisor
```

Install only the descriptor matching the host platform after replacing the sample
paths with that host's pinned release root and private state directories. Service
configuration and descriptors remain owner-only. Verify bounded restart, SIGTERM
drain, coordinator restart/rejoin, health, and log rotation on the installed hosts;
package generation alone is not runtime proof.

Build the deterministic reviewer bundle with
`scripts/build_astra_reviewer_bundle.py`. On a clean Mac, unpack it into an owner-only
directory and run `scripts/astra_reviewer_preflight.py` twice with the same unique
invitation. The second result must be an idempotent status read, not a second join.
Membership is not stage participation; placement, artifact load, and a physical
qualification must still pass before the reviewer host appears in an inference route.

## 10. Shutdown

Send `SIGINT` or `SIGTERM` to each supervisor and Device Lab server. Confirm all
native and browser worker sessions exit, all active KV state is zero, and every
digest-bound staging root is removed from its owning host. Do not delete a broad
directory, an unverified path, or a root belonging to another run.

## 11. Adding trusted users and devices

Do not use the loopback product UI to transmit native join credentials. For multiple
known testers or devices, follow `docs/swarm-multi-device-onboarding.md` and the
mandatory `docs/contracts/external-tester-boundary.md`. The operator workflow verifies
one durable live seed and mints unique owner-only, single-use bundles in a bounded
batch. A joined member remains `route_ready=false`; it cannot enter the active model
route until capability, placement, artifact load, runtime, and full physical
qualification all pass.
