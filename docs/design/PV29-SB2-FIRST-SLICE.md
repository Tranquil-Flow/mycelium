# PV29 SB2 first slice - runbook (parity gate v0 on the real mixed-backend route)

Lane L1 of `status/plans/PV29-LINUX-WINDOWS-PLAN-20261021.md` (§4 row L1, §5.2). Worktree
`~/Projects/mycelium-rel2-pv29-l1`, branch `lane/rel2-pv29-l1`. Written at the desk; **nothing in this runbook has been
executed on SB2**. Every command that touches SB2 is marked `[SB2]`; commands for the dev Mac are `[mac]`.

## 0. What this slice can and cannot claim

It can earn: *"A Linux x86_64 member (SB2, NumPy/CPU stage, Qwen2.5-0.5B) runs one stage of a mixed MLX+NumPy
route; the route passes parity gate v0 (H + Q0 + N) against the HF float32 oracle on the 8-case corpus; per-stage
compute ms/token and tok/s are recorded."* Gates G (generation consistency) and P (join-time probe) are
`not_evaluated` in v0, so the best possible verdict is **`partial_pass`** - say so every time it is quoted.

It does **not** claim: a Linux app, llama.cpp, GPU, Windows, performance eligibility, router-port parity (the gate
drives the production per-block kernels with stage-local KV and the real `MYAC` float32 frames, not the router ports'
leases/load proofs), or that the live network route was timed (see §6).

A FAIL naming a defect class is a successful gate outcome. Keep the verbatim output (§7).

## 1. How the cross-machine run works

Teacher-forced inputs are fixed (the corpus token ids), so stage k's hop frames do not depend on stage k+1. Each stage
therefore runs on its own machine and exchanges real `MYAC` v1 float32 frames as files; the numerics are those of a
live route. `scripts/run_backend_parity.py`:

| step | command | runs on | writes |
|---|---|---|---|
| stage 0 | `stage --index 0` | machine holding layers [0,12) | `hops0.npz` (frames, ~1-3 MB) |
| stage 1 | `stage --index 1 --frames-in hops0.npz` | machine holding layers [12,24) | `logits1.npz` (~117 MB: 192 positions x 151,936 f32) |
| judge | `evaluate --frames hops0.npz --logits logits1.npz` | machine with the weights + oracle | signed `record.json` |
| check | `verify --record record.json` | anywhere with the policy | exit 0 / 1 |

`logits1.npz` is large; device-to-device copies over the phone hotspot do not use mobile data, but client-to-client
speed is ~1 MB/s (skill `mycelium-physical-node-ops` §7) => ~2 min. Do not copy the 988 MB weights over it if SB2 already has them.

## 2. What to copy (do not download anything)

| item | size | source | destination | verify |
|---|---|---|---|---|
| Qwen2.5-0.5B-Instruct snapshot (`config.json`, `model.safetensors`; tokenizer files not needed) | 988 MB | Mac: `~/Library/Application Support/Mycelium A13 macOS Demo/model-source-7ae557604adf/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/7ae557604adf67be50417f59c2c2f167def9a775/` (also under `runs/v2/lane-a16-physical-20261018/runs/*/mac-agent/owner-app/model-source-7ae557604adf/`) | SB2 only if SB2 runs `evaluate` or a monolith; **stage 1 needs only its own layers but the script loads the whole file** | `shasum -a 256 model.safetensors` equal on both; first check whether SB2 still has it from August (`find ~ -name model.safetensors -path '*Qwen2.5-0.5B*'`) |
| HF float32 oracle `oracle-qwen05b-hf-f32.npz` + `.json` | 118 MB | Mac `~/.hermes/cache/scratch/pv29l1-work/` (regenerate in 5 s if gone, §4 step 1) | only the machine that runs `evaluate` | `archive_digest` in the `.json` (checked by the loader) |
| repo source (this lane's commit) | tracked tree only; **check `git archive HEAD \| wc -c` first** | Mac worktree | SB2 `~/pv29-l1/` via `git archive HEAD \| ssh mycelium-node2 'mkdir -p ~/pv29-l1 && tar -x -C ~/pv29-l1'` | `git rev-parse HEAD` printed on the Mac, compare the 3 new files by `shasum` |
| corpus + policy | ~10 KB each | in the repo (`tolerances/backend-parity-*`) | travel with the source | digests are printed by every command |

SB2 Python needs `numpy` (and `cryptography`, imported by `mycelium_platform_neutral` through
`mycelium_qualification.signing`, even for `stage`). Closure on SB2 is **unverified**: if `import numpy_runtime` or
`import mycelium_platform_neutral.backend_parity_route` fails, record the exact error and stop (abort rule 3); do not `pip install`
over the hotspot without a HEAD-size check (< 100 MB, plan §4.4).

## 3. Preflight and spec capture

`[mac]` quiet host (no certification/pytest running), AC power, `uptime` load < 2. Lease the devices with the fleet helper
(`~/Documents/mycelium-core-program/runs/v2/runday/fleet/lease.sh`; lease only the hosts you use - `mac-node` and `sb2`, never
`all`). Ask the owner whether SB2 is free all day (plan Q7).

`[SB2]` read-only spec capture (closes the "SB2 specs unknown" gap); paste the output into `runs/v2/pv29-sb2-slice-<date>/sb2-spec.txt`:

```bash
ssh mycelium-node2 'cat /etc/os-release; uname -r; lscpu | head -20; free -h; df -h ~; lspci | grep -iE "vga|3d"; nvidia-smi -L 2>&1; \
  stat -fc %T /sys/fs/cgroup; cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers; \
  cat /proc/pressure/memory; systemctl --user --version | head -1; command -v gcc cmake llama-server llama-cli; python3 --version; \
  python3 -c "import numpy; print(\"numpy\", numpy.__version__)"; python3 -c "import cryptography; print(\"cryptography\", cryptography.__version__)"; \
  grep -E "MemTotal|MemAvailable" /proc/meminfo; uptime'
```

Then free memory: `~/Documents/mycelium-core-program/runs/v2/runday/sb2_ha_pause.sh pause` (stops only the llama/nemotron user
units and the two queue timers; HAOS VM untouched; it prints `MemAvailable`).

**Abort** (record only the spec capture and stop): `MemAvailable < 3 GiB` after pause; clock skew > 0.5 s between hosts
(`date +%s` on both); SB2 cannot import `numpy_runtime` from the copied source after one retry.

## 4. The gate run

Set once (`[mac]`):

```bash
cd ~/Projects/mycelium-rel2-pv29-l1
PY=/Users/evinova-self/Documents/playground/mycelium-v2-m0-test-ladder/.venv/bin/python
SNAP="$HOME/Library/Application Support/Mycelium A13 macOS Demo/model-source-7ae557604adf/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/7ae557604adf67be50417f59c2c2f167def9a775"
W=$HOME/.hermes/cache/scratch/pv29l1-work; R=runs/v2/pv29-sb2-slice-$(date +%Y%m%d); mkdir -p $W $R
```

1. `[mac]` oracle (skip if `$W/oracle-qwen05b-hf-f32.npz` exists and its `.json` corpus digest equals the corpus digest). Needs a torch venv:
   `~/Projects/language-tutor/.venv/bin/python scripts/backend_parity_hf_oracle.py --snapshot "$SNAP" --corpus tolerances/backend-parity-corpus-qwen2.5-0.5b-instruct.v1.json --out $W/oracle-qwen05b-hf-f32.npz --boundaries 12`
2. `[mac]` **Route A - MLX stage 0 (Mac), NumPy stage 1 (SB2):**
   ```bash
   $PY scripts/run_backend_parity.py stage --snapshot "$SNAP" --backends mlx,numpy --index 0 --out $W/hops0-A.npz
   scp $W/hops0-A.npz mycelium-node2:pv29-l1/
   ssh mycelium-node2 'cd pv29-l1 && nice -n 19 python3 scripts/run_backend_parity.py stage --snapshot <SB2 snapshot dir> --backends mlx,numpy --index 1 --frames-in hops0-A.npz --out logits1-A.npz'   # [SB2]
   scp mycelium-node2:pv29-l1/logits1-A.npz $W/
   $PY scripts/run_backend_parity.py evaluate --snapshot "$SNAP" --oracle $W/oracle-qwen05b-hf-f32.npz --backends mlx,numpy --frames $W/hops0-A.npz --logits $W/logits1-A.npz --record-out $R/record-A.json
   $PY scripts/run_backend_parity.py verify --record $R/record-A.json --unanchored      # or --key-digest <printed by evaluate>
   ```
   `--backends mlx,numpy` is the **declared** route; the stage-1 command on SB2 executes the NumPy kernel for index 1 even though the
   string lists `mlx` for index 0 (index 0 is never built on SB2 and MLX is never imported there).
   `evaluate` takes each stage's `backend_version`, `build_digest` and `device_class` from the metadata written by the machine that ran the
   stage (and refuses frames/logits whose recorded backend differs from `--backends`), so the signed record names SB2's NumPy, not the Mac's.
3. **Route B (reverse) - NumPy stage 0 (SB2), MLX stage 1 (Mac):** swap roles with `--backends numpy,mlx`; stage 0 runs on SB2,
   `hops0-B.npz` is copied to the Mac, stage 1 and `evaluate` run on the Mac. This is the case that exercises SB2's NumPy numerics on
   the embedding side.
4. **Representations:** repeat 2 with `--reps int8-weight-only,int8-weight-only` (the "stage 0 on M4 Pro MLX int8, stage 1 on SB2 NumPy
   same quantization" configuration of plan §5.2). `evaluate` computes a same-representation NumPy monolith for gate H on the Mac.
5. `[mac]` also run the in-process Mac-only controls once for comparison: `run --backends mlx,numpy --battery $R/battery-mac.json`
   (the seeded-defect battery; every defect must fail, every control must pass).

Expected on a healthy SB2 NumPy stage: H pass, Q0 pass, N pass, G/P `not_evaluated`, verdict `partial_pass`. If H fails with
`hop_rel_l2_max`/`logits_max_abs_delta_top` only slightly over the threshold and the Mac-only controls are clean, SB2's BLAS has a
wider float spread than the Mac's: **do not edit the policy to make it pass** - record the measured spread (§5), keep the FAIL
verbatim, and let the owner decide whether to re-calibrate with SB2 samples (the policy is `provisional until the SB2 run`).

## 5. tok/s and timing record

Each `stage` prints and stores (`meta` inside the npz) `speed`: `prefill_ms_per_token`, `decode_ms_per_token_median`/`mean`,
`decode_tokens_per_second_stage_local` (= 1 / median decode step; **stage-local compute only, no network, no Python router
overhead, no batching**), `frame_encode_ms_per_decode_step_median`, plus `host`, `load`. Record per stage into
`$R/speed.md`: host, backend, representation, those numbers, `MemAvailable` before/after, load, thermals (`sensors` if present), link
(hotspot/LAN), RTT (`ping -c 20`). Do not quote stage-local tok/s as a route tok/s.

The plan's live-route measurement (64/512/2,048-token prompts, 128 out, ABBA vs the single-Mac baseline, >= 5 runs per arm,
per-stage decode ms/token, hop serialize/deserialize ms, TTFT, tok/s, RSS) needs the existing node/fleet path (plan Track G) and is
**not provided by this lane**; run it with the A5/A6 fleet machinery under its own lease and attach it as a separate evidence line.

Hypothesis only (plan §5.2): decode on a bandwidth-bound CPU stage <= memory bandwidth / stage weight bytes; compare the
measured SB2 `decode_tokens_per_second_stage_local` with that ceiling (float32 NumPy stage = ~0.8 GB of weights per 12 layers, so the
ceiling is much lower than for Q8_0).

## 6. Optional S1d (only if time and HEAD-checked size < 100 MB)

Whole-model stock llama.cpp CPU on SB2 (0.5B Q8_0 generated from the safetensors, or an already cached GGUF): `llama-bench`-style pp/tg
tok/s and top-1 agreement vs the oracle. Label it "whole-model stock llama.cpp, not a stage". Not part of the gate.

## 7. Evidence and restore

Archive under `runs/v2/pv29-sb2-slice-<date>/`: `sb2-spec.txt`, `record-A.json`, `record-B.json`, `record-*-int8.json` (signed),
`battery-mac.json`, the `stage` stdout (timings), the verbatim failure output if any, `git rev-parse HEAD` of the lane, the
`policy_digest`/`corpus_digest`/`oracle archive_digest` printed by `evaluate`, and `speed.md`. Then `[SB2]` `sb2_ha_pause.sh resume`
(it restarts exactly the units it stopped), `rm -rf ~/pv29-l1` on SB2 (leave no new units or files), release leases.

## 8. Known gaps / unverified (carry into the lane brief)

- `device_class` for SB2 is `cpu` (NumPy); the CPU model, RAM and OS are only in `sb2-spec.txt`, not in the signed record.
- SB2 Python version and whether `cryptography` and the repo import closure exist there are unverified.
- G and P are `not_evaluated`; the corpus has no case at position > 1,024 and only 192 scored positions (plan asks >= 512).
- The thresholds are `provisional`; see `tolerances/backend-parity-*` and the calibration report for how they were measured.
- The run drives kernels with stage-local KV in-process / by file replay, not the router ports and not the live iroh route.
