# Mycelium Serving Backend Bootstrap

`install_serving_backend.py` provides an explicit, platform-aware way to add a
local inference-serving runtime. It prints a dry-run plan by default and only
changes the current device when `--execute` is supplied.

The core Mycelium probe, broadcast, network, and planner modules remain
stdlib-only. This optional helper installs third-party serving dependencies.

## Recommended backend

| Target | Automatic choice |
|---|---|
| Apple Silicon | `mlx-lm` |
| NVIDIA Linux/Windows | `llama-cpp-python[server]` with `GGML_CUDA` |
| Other desktop CPU | `llama-cpp-python[server]` |
| Android inside Termux | native `llama-cpp` package |

The choices follow the upstream installation paths:

- MLX-LM: <https://github.com/ml-explore/mlx-lm>
- llama-cpp-python server: <https://github.com/abetlen/llama-cpp-python>
- llama.cpp Android/Termux: <https://github.com/ggml-org/llama.cpp/blob/master/docs/android.md>

## Usage

Inspect the local recommendation without installing:

```bash
python3 install_serving_backend.py
```

Inspect the recommendation for a captured profile:

```bash
python3 install_serving_backend.py --profile-file outputs/pixel8-adb-profile.json
```

Install on the current device:

```bash
python3 install_serving_backend.py --execute
```

Force a specific supported backend:

```bash
python3 install_serving_backend.py --backend llama_cpp --execute
```

The helper refuses `--execute` when a supplied profile describes a different
platform from the machine running the command. A remote profile is for planning
only; run the helper locally on the target device.

For Android, run it inside Termux. A normal ADB shell cannot modify Termux's
private package environment. The selected installation is equivalent to:

```bash
apt update
apt install -y llama-cpp
```

## After installation

Re-run `probe.py` on the device, or the Termux-native probe when available, then
announce the refreshed profile. The probe now recognizes both Python
`llama_cpp` and the `llama-cli`/`llama-server` binaries.

The bootstrap does not download model weights, choose a model, start a server,
or implement distributed layer execution.
