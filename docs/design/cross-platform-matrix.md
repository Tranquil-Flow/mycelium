# Cross-platform matrix: macOS-only assumptions in the node, seed, membership and gateway code

Status: **audit from source reading plus one simulated-platform import run.** No Linux or Windows hardware was
used. Every Linux and Windows cell below is **UNVERIFIED-on-hardware**. Line numbers are against this branch.

Evidence behind the cells:

- `scripts/linux_import_guard.py` (test: `tests/host_platform/test_linux_import_guard.py`) imports each package
  with `sys.platform == "linux"` and `objc`, `Foundation`, `AppKit`, `Quartz`, `mlx` and others unimportable.
  Result for `mycelium_node`, `mycelium_seed`, `mycelium_membership`, `mycelium_request_gateway`,
  `mycelium_platform_neutral`, `mycelium_host_platform`, `mycelium_a13_linux` plus `physical_inference_node`,
  `mycelium_service_runner`, `mycelium_service_package`, `mycelium_member_runtime`: 82 modules, 0 failures.
  `--all` (every `mycelium_*` package and top-level module, 545 modules) fails only on nine modules that import
  `mlx` at module scope, listed under "Import-time findings".
- The Windows equivalent used by the prephysical checks lives outside the repo
  (`runs/v2/runday/prephysical/win_import_guard.py`) and in-repo as
  `tests/host_platform/test_windows_import_surface.py`.
- "Works" means the code has a branch for that OS and a unit test with injected probes; it does not mean it was
  run there. The macOS app (`mycelium_a13_macos`) is macOS-only by design and is noted, not treated as a defect.

## Import-time findings (Linux, simulated)

| Module | Cause | Verdict |
|---|---|---|
| none in the node/seed/membership/gateway scope | | clean (UNVERIFIED-on-hardware) |
| `mycelium_router.mlx_runtime` (`:24`) | unconditional `import mlx.core` | MLX backend, expected |
| `qwen35_mlx` (`:40`) | unconditional `import mlx.core` | MLX backend, expected |
| `mycelium_interactive.runtime` (`:19`), `mycelium_interactive.server` (via runtime) | unconditional `import mlx.core` | Apple-only demo server |
| `mycelium_a11_draft_runtime` (`:39`) | unconditional `import mlx.core` | MLX draft runtime |
| `physical_pixel_host_stage` (`:16`) | unconditional `import mlx.core` | Apple-hosted Pixel stage harness |
| `distributed_inference_qualification` (`:20`), `two_process_inference_qualification` (`:29`), `two_process_runtime_qualification` (`:26`) | unconditional `import mlx.core` | Apple-only qualification harnesses |

No code was changed to make these importable: they are MLX consumers, and importing them on Linux would only move
the failure to first use. `MLX_ONLY_BY_DESIGN` in the guard pins this list, so a new unconditional macOS-only
import anywhere else fails `--all`.

Not covered by the guard (runtime, not import-time): `os.getuid`, `os.fchmod` and `O_NOFOLLOW` are POSIX-only
and fail at call time on Windows (see the Windows column).

## Component matrix

Legend: **works** = branch exists for the OS; **degraded** = runs but a signal or feature is absent or unknown;
**missing** = no implementation; **n/a** = out of scope by design.

| Component | macOS | Linux | Windows | Evidence |
|---|---|---|---|---|
| `mycelium_node` process supervision | works | works (procfs inventory) | degraded: the control-frame write has a win32 branch (`:410`), but process and group inventory raises `ProcessLookupError` off Linux/Darwin; Windows supervision goes through `mycelium_service_runner` and `mycelium_host_platform` job objects instead | `process.py:1342-1349`, `:1747-1752` (Linux/Darwin dispatch), `:1407,1706` (`/bin/ps`, Darwin helpers), `:2683` (`killpg`) |
| `mycelium_node` interpreter identity | works: framework `Python.app` image derived from `MYCELIUM_PHYSICAL_SERVICE_PYTHON` if set (macOS only), then Apple Silicon Homebrew `/opt/homebrew/bin/python3` (`_MACOS_LEGACY_PYTHON`, unchanged macOS behaviour), then plain `sys.executable` as the last fallback | works (`sys.executable`) | works (`sys.executable`, host platform) | `process.py:1186-1225`; tests `tests/node_agent/test_physical_interpreter_identity.py` |
| Node identity key and durable membership files | works | works | missing: needs `os.getuid`, `os.fchmod`, `O_NOFOLLOW`; Windows ACL path is `mycelium_a13_windows/acl.py` | `identity.py:154,160,176,188,249,313,324`; `durable_membership.py:162,168,216,221` |
| `mycelium_seed` (coordinator, state, authority, operator) | works | works | degraded: seed files use the same POSIX owner/mode checks; a Windows seed is not a stated goal | `authority.py:51,57,95`; `operator.py:59,66,260`; `invite_batch.py:61,72`; `__main__.py:305` |
| `mycelium_membership` | works | works | works (no OS calls) | no platform references |
| `mycelium_request_gateway` | works | works | works (no OS calls; HTTP/ASGI only) | no platform references in the package |
| `mycelium_platform_neutral` | works | works | works (`windows_provider.py`) | `native_int8.py:63` selects `WinDLL`; `thermal.py:126` per-OS dispatch |
| Resource and memory probes (`physical_inference_node`) | `vm_stat`, `sysctl vm.swapusage`, `pmset -g batt`, `ps` | `/proc/meminfo`, `/proc/self/statm`, `/sys/class/thermal`, `/sys/class/power_supply` | memory and RSS via host platform; swap, thermal and power fall through to unknown | `physical_inference_node.py:1172,1231,1260` (macOS), `:1156,1199,1216,1241,1268` (Linux), `:1153,1197` (Windows); thermal returns `None` off Linux (`:1252`) |
| A16 adaptive signals: host memory pressure | works (`sysctl`, `vm_stat`, `memory_pressure`) | works: `sample_linux_memory` reads PSI `some`/`full` `avg10` from `/proc/pressure/memory`, `MemAvailable` from `/proc/meminfo`, and cross-checks `MemAvailable/MemTotal` with the macOS bands; unreadable or malformed PSI (or a kernel without PSI) fails closed to `pressure: unknown`. PSI band thresholds are provisional, unmeasured | **missing by design**: no sampler, `HostSampler` reports `pressure: unknown` (fail closed) | `mycelium_a13_macos/adaptive_host.py:251` (macOS), `:328-380` (Linux), `HostSampler` `:418` dispatches on platform; tests `tests/host_platform/test_linux_memory_sampler.py` (fake file contents). CPU ticks stay macOS-only, so `cpu_utilization` is absent (unknown) on Linux |
| A16 signals: power, thermal, network policy | Objective-C probe `native/resource_probe.m` | `mycelium_a13_linux/managed_host/resources.py` (sysfs, NetworkManager) | `mycelium_a13_windows/managed_host/resources.py` (PowerShell/CIM) | `mycelium_a13_macos/resources.py:114-124` (dispatch), `:185-200` (build, darwin only) |
| Resource probe used by `probe.py` (dev tooling, not shipped node) | `sysctl`, `system_profiler`, `pmset` | partial (`/proc`, `rocm-smi`, `nvidia-smi`) | missing | `probe.py:91-92,99-163,224-328,436-470,547` |
| Service packaging | launchd plist rendered, `org.mycelium.<id>.plist` | hardened systemd unit (user service) | Windows SCM package | `mycelium_service_package.py:187,614-623`; `mycelium_a13_linux/service_lifecycle.py:8,165` |
| macOS service install by the app | **missing**: "not installed by this tranche yet" | n/a | n/a | `mycelium_a13_macos/installer.py:156,193` |
| Linux installer and managed host | n/a | works in source: per-user XDG layout, systemd user unit, desktop entry; admits Ubuntu >= 24.04 on x86_64/aarch64 only | n/a | `mycelium_a13_linux/installer.py:15-23,102-135`; `platform_admission.py:33-41` |
| Windows installer and managed host | n/a | n/a | works in source; admits Windows 10 build >= 19045 | `mycelium_a13_windows/platform_admission.py` |
| App install paths | `~/Library/Application Support/Mycelium/programs/<version>` | `${XDG_DATA_HOME:-~/.local/share}/mycelium/<version>` | per `mycelium_a13_windows/installer.py` | `mycelium_a13_macos/installer.py:23-26`; `mycelium_a13_linux/installer.py:15-19` |
| Packaging and signing | `codesign` ad-hoc, `ditto`, PyInstaller `.app` | PyInstaller on an Ubuntu 24.04 build host; no OS signing, a pinned Ed25519 manifest key instead | PyInstaller; Authenticode not in repo | `scripts/build_a13_macos.py:98-99`; `scripts/finalize_a13_macos.py:10,116`; `scripts/build_a13_linux.py:1-60`; `scripts/build_a13_windows.py` |
| Secret storage / Keychain | no Keychain use in node, seed, membership or gateway: the Ed25519 key is a 0600 file. Keychain appears only in design docs (signing identity) | same 0600 file | file with ACLs | `mycelium_node/identity.py:313-324`; docs `docs/superpowers/specs/2026-09-27-mycelium-node-app-architecture.md:217,236` |
| Owner-machine hard-codes | cutover default forbidden root is `<home>/mycelium-physical-run` plus `MYCELIUM_PRODUCTION_SEED_ROOTS` (fixed); `mycelium_demo_watchdog` and `mycelium_testbed/soak.py:1583-1585` still hard-code owner paths | cutover fixed | cutover fixed | `mycelium_platform_neutral/seed_cutover_transaction.py:29-44`; remaining: `mycelium_demo_watchdog/watchdog.py:34,36,88`, `commands.py:35,37,45`, `mycelium_testbed/soak.py:1583-1585` |
| Sidecar IPC | Unix socket (path-length limit noted) | Unix socket | loopback TCP with owner check | `mycelium_a13_macos/managed_runtime.py:138-143`; `native/iroh_transport` (not built here) |
| Single-instance and file locks | `fcntl.flock` | `fcntl.flock` | none (`fcntl = None` fallback, no lock) | `mycelium_a13_macos/control_pump.py:10-12,153`; `control_session.py:11-13,177`; `mycelium_platform_neutral/migration_executor.py:18` |
| The A13 app itself | works; Apple Silicon and an admitted macOS only | n/a (separate `mycelium_a13_linux` host, but it imports the `mycelium_a13_macos` modules for control logic) | n/a | `mycelium_a13_macos/app.py:96-121`, `capability.py:39,114`, `__main__.py:98` (`osascript`), `:115,178` |

## Backend matrix (what exists in this repo)

| Backend | Where | macOS Apple Silicon | Linux | Windows | Notes |
|---|---|---|---|---|---|
| MLX (stage runtime) | `mycelium_router/mlx_runtime.py`, `runtime_loader.MLXStageBackend`, `qwen35_mlx.py` | works | **missing** (unconditional `import mlx.core`) | **missing** | the only real-model stage executor |
| MLX int8 | `mycelium_platform_neutral/mac_mlx_int8.py` (lazy `import mlx.core`, `:42`) | works | missing | missing | lazy import: safe to import elsewhere |
| NumPy CPU | `mycelium_router/numpy_runtime.py`, `numpy_runtime.py`, `runtime_contracts.py` | works | works (this is what `mycelium_a13_linux` declares: backend `numpy`, precision `float32`, `managed_host/member.py:25,92-94`) | works (A9 route runs mlx on macOS, numpy on Windows per `scripts/run_a9_windows_route.py`) | CPU only; docs say no CUDA or MKL claims; small models |
| Native int8 CPU kernel | `native/a9_int8_kernel` (C), loaded by `mycelium_platform_neutral/native_int8.py` | buildable (`build_posix.sh`) | buildable; dispatch `avx2_fma` or `scalar` only | `build_windows.cmd` | ctypes load, digest-pinned; `SUPPORTED_DISPATCH` at `native_int8.py:30` has no NEON path |
| llama.cpp server (HTTP) | `mycelium_router/llama_runtime.py` | works against `/opt/homebrew/bin/llama-server` (docstring `:5`) | thin HTTP client, runs wherever `llama-server` runs | same | whole-model GGUF, not a layer-range stage executor; no stage-slicing in this adapter |
| `install_serving_backend.py` | picks `mlx_lm`, `llama_cpp`, `llama_cpp_cuda` (`-DGGML_CUDA=on`, `:115,146`), `llama_cpp_termux` | works | CUDA build option for NVIDIA | not covered | installer helper, outside the distributed-stage path |
| `layer_planner.py` backend names | `cuda`, `torch_cuda`, `torch_mps`, `mlx`, `llama_cpp`, `mlc_llm`, `onnxruntime` (`:33-42,468`) | | | | planner vocabulary and weights only; no executor in the repo behind CUDA, Vulkan, ROCm or DirectML |
| `windows_provider.py` | lists `directml` among provider names (`:72`) | | | detection vocabulary | no DirectML executor found |
| Vulkan | none found | | | | no code in the repo |
| CUDA stage executor | none found; only `install_serving_backend.py` and `probe.py:219` detection | | | | no stage executor |

Evaluated and not selected (`docs/runtime-backend-selection.md`): ONNX Runtime, Torch, ExecuTorch, WebGPU.

## Ranked task list

Ranked by user impact per risk. The first two are the only ones that block a Linux or Windows node from taking
part in the A16 policy loop or a shipped install.

1. **Windows A16 memory-pressure sampler.** Linux is done (`adaptive_host.sample_linux_memory`: PSI +
   `MemAvailable`, fake-file tested; thresholds still need hardware to validate). Windows needs a
   host-platform equivalent; until then it reports `pressure: unknown`.
2. **Real layer-range executor for non-Mac hosts** (R2 T13). `llama_runtime.py` is a whole-model HTTP client;
   NumPy is the only stage runtime off Apple. Define the backend behind `StageRuntimeBackend` with a NumPy
   conformance test before any engine. Large; numerics risk across backends.
3. **Windows identity and seed file checks.** `identity.py`, `durable_membership.py` and the seed files call
   `os.getuid`/`os.fchmod`. Route through `mycelium_host_platform.private_directory` or an ACL check, as
   `process.py:879` does. Only matters if a Windows member runs the `mycelium_node` agent directly.
4. **Remove the remaining owner-machine hard-codes** (R2 T15): watchdog and testbed paths. The cutover root
   and `process.py`'s `/opt/homebrew/bin/python3` assumption are fixed (home-derived root, `sys.executable`
   or `MYCELIUM_PHYSICAL_SERVICE_PYTHON`; macOS order unchanged: configured, Homebrew, then `sys.executable`).
5. **Wire the Linux import guard into CI** as an `ubuntu-latest` job (R2 T12): run
   `python -B scripts/linux_import_guard.py` and `--all`. Add `cargo check --target x86_64-unknown-linux-gnu`
   (needs the target installed; not run here).
6. **macOS launchd user agent** in `mycelium_a13_macos/installer.py:156,193` (the plist renderer exists in
   `mycelium_service_package.py:187`).
7. **Lock fallback without `fcntl`** in `control_pump.py` and `control_session.py` (Windows runs unlocked today);
   use `mycelium_host_platform` exclusive lock (`tests/host_platform/test_exclusive_lock.py` exists).
8. **Widen Linux admission** beyond Ubuntu >= 24.04 (`platform_admission.py:33-41`) after a smoke run on each
   distribution. WSL and non-systemd hosts are refused today.
9. **Intel Mac and Linux/Windows "device" copy in the app** (R2 T11): `app.py:96-121` refuses non-arm64 Macs.
10. **Add NEON to the native int8 kernel dispatch** for aarch64 Linux; only `scalar` applies there today.
11. **Make the MLX demo and qualification harnesses import without MLX** (the nine modules above) only if a
    non-Mac developer needs to run their other code paths; low value because they need MLX to do anything.
