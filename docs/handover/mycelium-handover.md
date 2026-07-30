# Mycelium MVP — Autonomous Build Prompt for Moonsong

> Hand this to Moonsong on m4pro (evinova-self, 48GB, Tailscale `tranquil.flowstate@`).
> Moonsong: caveman style. Keep messages to Astra short. This is a single autonomous session — do not stop until devices are connected or blocked.

## First Message — Status + Device Request

Send to Astra:
```
Mycelium MVP status:
- 11 lanes implemented and reviewed. 2 repos integrated.
- DIMVP integration: 2,691 tests pass
- Mycelium integration: 2,408 tests pass
- Physical distributed inference test: ready to build once devices connected

Need your help — enable these devices so I can test:

1. Astra MacBook:
   Run in terminal: `whoami`
   Send me the output.
   Then: System Settings → General → Sharing → Remote Login (ON)
   Keep awake, plugged in, Tailscale connected.

2. Pixel 8 Pro:
   Settings → Developer Options → Wireless Debugging (ON)
   Keep screen unlocked.
   IP: 100.126.233.4
```

## Once Devices Connected — Autonomous Build & Test

### Phase 1: Verify connectivity
```bash
# Astra — user will provide IP/identity
ping -c1 <astra-tailscale-ip>
ssh <astra-user>@<astra-tailscale-ip> 'echo ok'

# Pixel
ping -c1 100.126.233.4
# Verify ADB: adb connect 100.126.233.4
```

### Phase 2: Deploy workers to each device

Each device needs Python 3.14+ and the repo. For M4 (already set up): use `/opt/homebrew/bin/python3`. For Astra: install or use virtualenv. For Pixel: deploy via ADB, use Termux Python.

Key files that must be present on every device:
- `physical_pixel_host_stage.py` — worker entrypoint
- `runtime_loader.py`, `numpy_runtime.py` — execution runtime
- `mlx_runtime.py` — MLX runtime (M4/Astra only)
- Model weights: tiny GPT-2 (or use already-present weights)

### Phase 3: Build distributed execution graph

Read these files for context:
```
~/Projects/mycelium-cron-lane24/two_process_inference_qualification.py  (1644 lines — reference architecture)
~/Projects/mycelium-cron-lane24/physical_pixel_host_stage.py  (228 lines — Pixel worker)
~/Projects/.mycelium-plan/MVP_COMPLETION_PLAN.md  (physical gates section)
```

The adaptation path:
1. Start from `two_process_inference_qualification.py` as template
2. Replace `multiprocessing.spawn` → iroh transport connections (each device runs independently, connects via Tailscale)
3. Replace `LoopbackSocketMesh` → `IrohSocketMesh` (already implemented in the iroh transport)
4. Replace `_MLXWorkerProxy` pipe RPC → iroh stream RPC
5. Keep model sharding, activation encoding, token decoding logic — those are transport-agnostic
6. Write a driver script at `~/Projects/mycelium-cron-lane24/run_distributed_inference.py`

Success criteria:
- Model loaded across M4 + Astra + Pixel (any distribution of layers)
- Real GPT-2 token generation (any prompt → coherent output tokens)
- Latency measured per-stage
- All devices contribute (traffic visible on all three)
- No hardcoded demo paths — uses actual weights and routing

### Phase 4: Verify and report

Run the driver. Report:
- Devices connected and contributing
- Model layers per device
- Token output (first 20 tokens)
- Per-stage latency
- Any failures or timeouts

### If blocked at any phase

Report exact blocker to Astra. Do not fabricate results. Do not push to GitHub.

---

## Reference: Integration Map

| Repo | Branch | HEAD | Tests |
|------|--------|------|-------|
| DIMVP | `integration/wave3-serial` | `8fded03` | 2,691 pass |
| Mycelium | `integration/wave3-mycelium` | `2109b95` | 2,408 pass |

Base commit for all work: `b317566` ("inert physical qualification controller").

### Lane inventory

| Lane | Branch | HEAD | Files |
|------|--------|------|-------|
| A7 E2E | `feat/session-2.3b-local-e2e-v2` | `9e2071a` | iroh transport, E2E tests |
| A3 runtime | `feat/session-3b5-runtime-completion` | `49d0f0b` | optional MLX, NumPy backend |
| A4 swarm | `feat/overnight-2.4-swarm-extend` | `d62871a` | browser on membership plane |
| A6 gateway | `feat/overnight-3b4-request-gateway` | `b4676c6` | route-gated request admission |
| A1 R1 | `fix/a1-durable-heartbeat-v5` | `ad875f6` | crypto-bound heartbeat |
| A1 R2 | `fix/a1-endpoint-secret-conduit` | `28a824d` | endpoint-secret conduit |
| A5 | `feat/overnight-3b1-planner-placement` | `2cabd0e` | planner placement (WIP, 7 RED tests) |
| A8 | `f04e438` in sess-a8-controller | — | physical qualification controller |

### Key tools & paths
- Python: `/opt/homebrew/bin/python3`
- Suite lock: `~/Projects/.mycelium-plan/locks/full-suite.lock`
- Plans: `~/Projects/.mycelium-plan/`
- Worktrees: `~/Projects/mycelium-sess-*`, `~/Projects/mycelium-cron-*`
