# Pilot service runbook: install, upgrade and recovery for the packaged Mycelium services

**Scope.** This is the operator procedure for the three durable services the pilot packages and ships:
`org.mycelium.node`, `org.mycelium.seed` and `org.mycelium.supervisor`. It covers installing them from a
recorded commit, upgrading them without losing state, recovering from the failure modes the pilot has
actually observed, and collecting evidence after an incident.

**Provenance.**
- Repository: `~/Documents/playground/mycelium-core-pilot`, branch `integration/core-pilot`.
- Artefacts: the readiness gate's packages, built from the committed bytes at
  `git_head = a5437e2f3e5269a77a4b214a4406c07ec320048e`, recorded in
  `runs/wp2-final-gate/PACKAGE.json` (`package_digest = sha256:67ca15d29dfeb1ce0a4fb22914d5789ebeffddfd4278824f7b30204fe81abaee`).
- Evidence for every command in this file: `runs/wp9-2/` (index in §0.2).
- Owner of the un-executed parts: WP5. See §7.

**Closure-train review (2026-09-21).** An independent draft of this runbook exists at
`runs/wp9-2-draft/pilot-service-runbook.md` (795 lines, packet wp9-2, written 2026-09-20) and was diffed
against this file line by line. It is a **parallel pass over the same recorded commit, not a replacement**:
its extra bulk is reflowed prose, renumbered sections, and its own evidence index for the *same*
observations (its `[PRIOR]` labels re-cite `runs/wp9-2/`). Only the deltas below were source-supported and
are now in this file; the rest was deliberately not taken. Evidence for the review pass itself:
`runs/20260921-closure-train/wp9/`.

| Draft delta | Verdict | Basis |
|---|---|---|
| Duplicate-frame mechanism in §5.5 | **taken** — the draft's correction is the a4-at-most-once RCA's own correction | `rca/wp2-8-at-most-once-duplicate-frame.md`, "Correction (2026-09-16)" |
| Recorded-commit build shape in §3.1 | **taken** | re-verified here: `git archive a5437e2 \| tar -t` = 1390 files (`02-recorded-commit-closure.txt`) |
| Package-digest rollup derivation in §3.2 | **taken** | `scripts/pilot_readiness_gate.py:1310-1321`, read at source |
| Terminate-path line citations in §4.1/§4.3 | **taken** | `mycelium_service_runner.py:102`, `:111`, `:188`, read at source |
| Its re-observations of *this* host (launchctl/pmset samples, demo pid churn) | **not taken** | same evidence `runs/wp9-2/` already carries, at a different timestamp |
| Its `NOT EXECUTED` ledger and label scheme | **not taken** | §0.1/§0.2 are this file's ledger; two ledgers in one document is worse than one |

Re-verified while reviewing (unchanged in this file, listed in `runs/20260921-closure-train/wp9/`): all 12
recorded payload digests, sizes and modes still match the gate tree (`01-digest-verify.txt`); the three
`release/service-configs/*.json` blobs are byte-identical at `a5437e2` and at this worktree's HEAD
`e23d243` (`02-recorded-commit-closure.txt`); `tests/path_cancellation_adversarial/test_at_most_once_dispatch.py`
is green (`03-at-most-once-gate.txt`); `scripts/claim_boundary_audit.py` is `ok: true`
(`04-claim-boundary-audit.txt`).

**Why there is a "not executed" column everywhere.** Everything in this runbook that can be exercised
without touching the live machine was exercised against the gate-built packages in a scratch directory.
Anything that would need `sudo`, a real `launchctl` load, a root-owned path, or the live hackathon demo
was **not** run and is labelled with the reason. Nothing in this file was written from memory.

---

## 0. How to read this runbook

### 0.1 Labels

| Label | Meaning |
|---|---|
| `[RAN]` | executed on this host; the raw output tail is in `runs/wp9-2/<file>` (named inline) |
| `[READ]` | read from repo source or a doc, cited `file:line` |
| `[NOT EXECUTED - <reason>]` | the step needs the live system, root, or WP5 material; the reason is stated |

Every command in this file is one of those three. There are no commands in this file that were neither
run nor explicitly marked.

### 0.2 Evidence index (`runs/wp9-2/`)

| File | What it proves |
|---|---|
| `01-env.txt` | host/interpreter/launchd baseline, worktree head, absence of `/opt/mycelium`, `/var/lib/mycelium`, `/var/log/mycelium` |
| `02-preflight-not-installed.txt` | `org.mycelium.{node,seed,supervisor}` are not registered; the four live `now.mycelium.*` demo agents are |
| `03-digest-verify.txt` | all 12 recorded digests match the bytes on disk, mode 0600 (`RESULT: PASS`) |
| `04-plist-and-unit-keys.txt` | `plutil -lint` OK and the full key dump for all three plists; systemd unit policy lines |
| `05-build-scratch-root.txt` | the canonical packager rebuilds all three packages into a scratch root (exit 0) |
| `06-reproduce-check.txt` | rebuilds are byte-identical once the embedded output root is normalised; only the output root is free |
| `07-interpreter-hazard.txt` | the shipped `argv[0]` (`/usr/bin/python3`, 3.9.6) cannot import the repo: `TypeError ... 'slots'` |
| `08-runner-rehearsal.txt` | the product's own runner: clean exit, bounded crash restart, wedge, stop escalation, config rejection |
| `09-upgrade-rollback-rehearsal.txt` | an upgrade changes only `service-config.json` (+manifest); rollback restores the previous digests |
| `10-power-thermal-detection.txt` | sleep/thermal/assertion detection commands and their current values |
| `11-launchd-detection-surface.txt` | `launchctl list`/`print-disabled`, the launchd log predicate, and health probes on this host |
| `12-soak-trace-evidence.txt` | which command writes lifecycle traces, where they land, and how to read duplicates |
| `13-launchd-manpage-semantics.txt` | the local `launchd.plist(5)` text for `SuccessfulExit`, `ThrottleInterval`, `ProcessType`, and the no-op keys |
| `14-stale-bound-detection.txt` | `file:line` handles for a captured-at-startup bound/address |
| `15-install-shape-rehearsal.txt` | a real seed worker brought up through the package's own runner in a scratch root (health 200, then stopped) |
| `16-install-preconditions.txt` | the state-directory precondition (owner + mode 0700) and the node's failure today |
| `17-running-bytes-and-wedge.txt` | the unit→runner→config→worker chain by pid, and the wedge detector calibrated against a real worker |
| `18-restart-budget-persistence.txt` | restarting the unit does not clear an exhausted restart budget |
| `19-demo-log-growth.txt` | the demo agents' unrotated logs measured today (19.9 MiB / 3.4 MiB, no rotated siblings) and their crash-loop rows |

The scene scripts that produced 03, 06, 08, 09, 15, 17 and 18 are kept next to the outputs
(`verify_package_digests.py`, `reproduce_check.py`, `rehearse_runner.py`, `rehearse_upgrade.py`,
`rehearse_install_shape.py`, `rehearse_running_bytes.py`, `rehearse_budget_persistence.py`) so a later
reader can re-run them; scratch material is under `runs/wp9-2/scratch/`.

### 0.3 Never touch these

This host is the owner's working machine and runs the live hackathon demo. Do not install, load,
bootout, unload, kickstart or signal any real `launchd` unit while following this runbook, and in
particular never touch the six `now.mycelium.*` agents, the `mycelium-demo` cloudflared tunnel, or
`127.0.0.1:4351`. `[RAN]` the four live ones are visible in `02-preflight-not-installed.txt`; the demo
answered `403` on `127.0.0.1:4351` while this runbook was written (`11-launchd-detection-surface.txt`).
Every rehearsal below is a child process in a scratch directory, never a unit.

---

## 1. What this host looks like today

`[RAN] 01-env.txt, 02-preflight-not-installed.txt`

- macOS 26.5.1 (25F80), Darwin 25.5.0, `launchd` Darwin Bootstrapper 7.0.0, login domain `gui/501`.
- `/usr/bin/python3` is **3.9.6**; `/opt/homebrew/bin/python3` is **3.14.4**. The repo's docs pin 3.14.
- `/opt/mycelium`, `/var/lib/mycelium` and `/var/log/mycelium` **do not exist** (the shipped configs
  point at all three).
- No pilot service is registered: `launchctl print gui/501/org.mycelium.node` →
  `Could not find service "org.mycelium.node" in domain for user gui: 501` (exit 113), same for `seed`
  and `supervisor`.
- The live demo's agents are registered and visible: `now.mycelium.free-app`, `now.mycelium.paid-app`,
  `now.mycelium.edge`, `now.mycelium.tunnel`. Two of them show a non-zero status column with no live pid
  — the crash signature §5.1 detects.
- `launchctl print-disabled gui/501` also lists `"org.mycelium.a2-membership-node-0" => enabled`, which
  is **not** one of the pilot's labels (different naming line). Do not mistake it for a pilot service.

---

## 2. The artefact you are installing

`[RAN] 03-digest-verify.txt`, `[RAN] 04-plist-and-unit-keys.txt`, `[READ] mycelium_service_package.py:151-167`

Three packages, four files each, built by `scripts/package_m22_service.py` from
`release/service-configs/{node,seed,supervisor}.json`:

| Role | Payload digests (from `runs/wp2-final-gate/PACKAGE.json`) |
|---|---|
| node | plist `7d5308b5…e4c0d656`, unit `4fcc8b04…ee406278`, config `34e93d77…5a9b8b86`, manifest `4e47410b…efd215f5` |
| seed | plist `8e017d3c…f43256df`, unit `079d22ff…fa8251f5`, config `820ce52c…c5e6481c`, manifest `3179526e…1ec39ac4` |
| supervisor | plist `81c97126…676a4be2`, unit `a905933a…f44e3007`, config `a23e0ad6…f54b1010`, manifest `a8ec774c…4882e020` |

The full 64-character digests are in `PACKAGE.json` and repeated verbatim in `03-digest-verify.txt`.
The fourth file, `service-package-manifest.json`, records the other three; it cannot contain its own
digest, so the manifest's own digest appears **only** in `PACKAGE.json` — compare it there, not against
the manifest. (This is why an earlier version of the verifier reported a failure; the corrected check is
the one in `03-digest-verify.txt`, `RESULT: PASS`.)

The launchd plist the packager emits, for every role:

```
Label              org.mycelium.<service_id>
ProgramArguments   [<argv[0] from the config>, -B, -m, mycelium_service_runner,
                    --config <OUTPUT-ROOT>/service-config.json]
WorkingDirectory   /opt/mycelium/current
EnvironmentVariables  PYTHONDONTWRITEBYTECODE=1, PYTHONPATH=/opt/mycelium/current
RunAtLoad          true
KeepAlive          { SuccessfulExit: false }        <- see §5.2
ThrottleInterval   5                                = the config's restart_delay_seconds
ProcessType        Background
StandardOutPath    /var/log/mycelium/<service_id>.stdout.log
StandardErrorPath  /var/log/mycelium/<service_id>.stderr.log
SoftResourceLimits { NumberOfFiles: 4096 }
```

The systemd twin carries the same policy: `StartLimitIntervalSec=300`, `StartLimitBurst=5`,
`Restart=on-failure`, `RestartSec=5`, `KillSignal=SIGTERM`, `TimeoutStopSec` 45 (node, seed) / 90
(supervisor), `NoNewPrivileges=true`, `UMask=0077`.

Two consequences of the recorded bytes decide the whole procedure:

1. **The plist and unit embed the absolute output root** as the `--config` path
   (`04-plist-and-unit-keys.txt`; `PACKAGE.json.build.note`). `[RAN] 06-reproduce-check.txt` rebuilds the
   packages into a different scratch root and shows the plist/unit/manifest digests change, while
   `service-config.json` is byte-identical at every root, and that normalising the embedded root back to
   the recorded one reproduces the recorded digest exactly. So: **build into the recorded output root, or
   record a new digest.** An unrecorded root is a different package.
2. **`argv[0]` of the shipped configs is `/usr/bin/python3` (3.9.6 on this host) and it becomes the
   interpreter of the installed unit.** `[RAN] 07-interpreter-hazard.txt` shows
   `/usr/bin/python3 -B -m mycelium_service_runner` dying at import with
   `TypeError: dataclass() got an unexpected keyword argument 'slots'` (`mycelium_qualification/contracts.py:124`),
   and `[RAN] 15-install-shape-rehearsal.txt` runs the package's own `ProgramArguments` verbatim and gets
   exit 1 with that traceback. **Installing these packages unchanged on a host whose `python3` is below
   3.10 produces a unit that never starts; see §3.7.**

`ThrottleInterval` is **5**, not the 10 in `research/research-launchd.md`: that note measured a different
config (`restart_delay_seconds=10` for a `core-pilot` node plist). The number that binds here is the one
in the recorded bytes, i.e. the config's `restart_delay_seconds`.

---

## 3. Install

### 3.1 Build from the recorded commit

`[RAN] 05-build-scratch-root.txt` (exit 0 for all three roles)

```sh
cd ~/Documents/playground/mycelium-core-pilot
git rev-parse HEAD                     # must be the commit you are installing
/opt/homebrew/bin/python3 -B scripts/package_m22_service.py \
  --config release/service-configs/node.json \
  --output-root <OUTPUT-ROOT>/node
# repeat for seed.json and supervisor.json
```

The packager prints the package document as canonical JSON on stdout (that is the manifest body) and
exits 0. `--output-root` must be absolute; it is the root recorded in `PACKAGE.json`, because the plist
points at `<OUTPUT-ROOT>/service-config.json`. Confirm the configs you are building are the committed
ones (`git hash-object release/service-configs/node.json` → `f027fd66…` for node, `86bcab86…` for seed,
`4bd68e22…` for supervisor — `05-build-scratch-root.txt`).

To be sure you are building the **recorded bytes** rather than whatever HEAD happens to be, build from the
recorded commit's own tree instead of from the checkout. The packager's import closure is not just its own
files, so extract the whole tree:

```sh
git archive a5437e2f3e5269a77a4b214a4406c07ec320048e | tar -x -C <scratch-tree>
/opt/homebrew/bin/python3 -B <scratch-tree>/scripts/package_m22_service.py \
  --config <scratch-tree>/release/service-configs/node.json \
  --output-root <OUTPUT-ROOT>/node
```

`git archive a5437e2f3e5269a77a4b214a4406c07ec320048e | tar -t` lists **1390 files**
(`runs/20260921-closure-train/wp9/02-recorded-commit-closure.txt`), which is why the closure matters.
Building that tree's three configs into a scratch root reproduces every recorded digest
(`runs/wp9-2-draft/13-recorded-commit-build.txt`, `RESULT: PASS`). The same file records that the three
`release/service-configs/*.json` blobs are byte-identical at `a5437e2` and at the current worktree HEAD, so
today the two build shapes agree — but the `git archive` shape is the one that keeps agreeing when HEAD
moves.

### 3.2 Verify the recorded digests before installing

`[RAN] 03-digest-verify.txt` — `RESULT: PASS` for all 12 files, and every file is mode 0600.

```sh
/opt/homebrew/bin/python3 -B runs/wp9-2/verify_package_digests.py \
  ~/Documents/mycelium-core-program/runs/wp2-final-gate
```

It compares `PACKAGE.json` → each `service-package-manifest.json` → the bytes on disk, and checks size
and mode. Read a failure as "stop and re-record", never as "close enough".

If you ever have to recompute the gate-level rollup itself, its derivation is: `sha256` over, for each role
in sorted role order, `role\0`, then for each file in sorted path order `path\0sha256\n`
(`[READ] scripts/pilot_readiness_gate.py:1310-1321`, `_package_digest`). Rebuilding at a different output
root changes it, because the plist and unit embed the root (§2, fact 1).

Also validate the plists before copying them anywhere:

```sh
plutil -lint <OUTPUT-ROOT>/node/org.mycelium.node.plist     # -> OK   [RAN] 04
plutil -p    <OUTPUT-ROOT>/node/org.mycelium.node.plist     # key dump [RAN] 04
```

### 3.3 Pre-install preconditions (what the shipped configs assume)

| Precondition | Status on this host | Evidence |
|---|---|---|
| `/opt/mycelium/current` exists and holds the code (`PYTHONPATH`, `WorkingDirectory`) | absent | `01-env.txt` |
| `/var/lib/mycelium/{node,seed}` exists, owned by the agent's user, mode 0700 | absent | `01-env.txt`, `16-install-preconditions.txt` |
| `/var/log/mycelium` exists and is writable by the agent's user | absent | `01-env.txt` |
| `argv[0]` is an interpreter ≥ 3.10 with the repo importable | **3.9.6 — fails** | `07-interpreter-hazard.txt` |
| supervisor: `ui/web/dist` static root and `/var/lib/mycelium/operator-plan.json` | absent (WP5-owned) | `release/service-configs/supervisor.json` |

Creating the root-owned paths is **[NOT EXECUTED - requires root; this packet forbids `sudo` and the
host is in active use by the live demo]**. The equivalent is done in a scratch root instead
(`15-install-shape-rehearsal.txt`).

The state directory precondition is a real, observed rule, not a guess:

- `[RAN] 16-install-preconditions.txt` runs the shipped seed argv by hand with a missing state dir →
  exit 2, `seed_preflight_failed`; with the dir at mode **0755** → exit 2, `seed_preflight_failed`;
  after `chmod 700` → exit 0 and the JSON init line, with `identity` and `state.sqlite3` created.
  `[READ] mycelium_node/process.py:546-556` is the check: the final component must be a directory owned
  by the running user with mode exactly 0700.
- The mode is self-healing **when the runner starts the worker**, because the runner chmods its state
  directory to 0700 itself (`[READ] mycelium_service_runner.py:24-27`); the *owner* is the part an
  operator must get right (`/var/lib/mycelium/<role>` must belong to the user the agent runs as).

### 3.4 Preflight the worker through the package (no launchd)

`[RAN] 15-install-shape-rehearsal.txt`, `[RAN] 17-running-bytes-and-wedge.txt`

This is the step that catches most installation mistakes before a unit exists. Build the package into a
scratch root, point a *copy* of the config at scratch state/log/work dirs and a real `PYTHONPATH`, then
start the product's own runner by hand:

```sh
/opt/homebrew/bin/python3 -B -m mycelium_service_runner --config <scratch>/service-config.json
```

Observed in that rehearsal: the runner writes
`<state_directory>/.mycelium-service-seed.json` with `state=running`, spawns
`python -m mycelium_seed --bind 127.0.0.1 --port 8876 …`, and `GET /seed/identity` returns **200**. The
same script shows the two other install facts: the packaged `ProgramArguments` verbatim crash-loops on
3.9.6 (§3.7), and `mycelium_seed --init-only` writes the identity material into the state dir before the
first real start.

### 3.5 Install the units

`[NOT EXECUTED - installing/loading a real launchd unit is forbidden by this packet, and the
preconditions in §3.3 are unmet]`. The command shape is the repo's own precedent
(`[READ] docs/release/external-mac-reviewer.md:20-24`), with `SERVICE_ID` ∈ {node, seed, supervisor}:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp "<OUTPUT-ROOT>/<role>/org.mycelium.SERVICE_ID.plist" \
   "$HOME/Library/LaunchAgents/org.mycelium.SERVICE_ID.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.mycelium.SERVICE_ID.plist"
launchctl enable    "gui/$(id -u)/org.mycelium.SERVICE_ID"
launchctl kickstart -k "gui/$(id -u)/org.mycelium.SERVICE_ID"
```

`bootstrap` loads it, `enable` persists the enable state (`launchd.plist(5)` notes the disable state is
kept externally), `kickstart -k` starts it. Do **not** run these on this host.

### 3.6 What "up" means, observably

| Role | "up" is observable as | Source |
|---|---|---|
| seed | `curl -sS -m 2 -o /dev/null -w '%{http_code}' http://127.0.0.1:8876/seed/identity` → `200` (signed envelope body) | `[RAN] 15`, `health_url` in `seed/service-config.json` |
| supervisor | same probe against `http://127.0.0.1:8791/__mycelium/live-status` → `200` | config; `[NOT EXECUTED - needs the installed service and the UI static root]` |
| node | **no HTTP endpoint** (`health_url: null`): the observable is the runner state file `state=running` plus a live pid; the node also prints nothing when healthy (`[READ] mycelium_node/__main__.py:1059-1062` prints only failure codes) | `node/service-config.json`, `[RAN] 17` |
| all | `<state_directory>/.mycelium-service-<service_id>.json` → `{"state":"running","attempt_count_in_window":N,…}` | `[RAN] 08`, `15`, `17` |

```
# which pid is the unit's program, and which pid is the worker it owns
ps -ww -o pid,ppid,lstart,command -p <supervisor pid>
pgrep -P <supervisor pid>
```

A healthy **log line** exists only for the seed (the worker prints a startup event on stdout, which the
plist redirects to `/var/log/mycelium/seed.stdout.log`). `[RAN] 17-running-bytes-and-wedge.txt` captured
it; its fields are `event=seed_started`, `protocol=mycelium.seed_main_status.v1`, a readiness boolean
that is `false` in the rehearsal, the seed endpoint id, and `seed_url=http://127.0.0.1:8876` — see the raw
file for the exact bytes. For the node and supervisor a *frozen or empty stdout is normal*, not a fault:
`research/research-launchd.md` §4.15 measured 20,039 s of healthy stdout silence from a live agent. Use
the state file and the health probe, not the log, to decide liveness.

### 3.7 The crash-loop you get if you skip §3.3

`[RAN] 15-install-shape-rehearsal.txt` step 2; `[RAN] 07-interpreter-hazard.txt`

Running the package's own `ProgramArguments` gives exit 1 with
`TypeError: dataclass() got an unexpected keyword argument 'slots'` at
`mycelium_qualification/contracts.py:124`. A non-zero exit is a *crash* to launchd, and
`KeepAlive{SuccessfulExit:false}` restarts crashes, so the unit would restart every
`ThrottleInterval` (5 s) forever — and because the failure is inside the runner process itself, the
runner never reaches its own budget and no state file is written. Before installing, either make
`argv[0]` an interpreter ≥ 3.10 (the repo pins 3.14 — the guard is `release/python-requirements.lock`
and `README.md:79`) or ensure the target host's `python3` satisfies the floor. This is a config decision
that must land as a recorded package (see §8, risk 1), not as a hand edit on the installed file.

---

## 4. Upgrade

### 4.1 Safe order

1. Record what is installed: `shasum -a 256` (or `03`'s verifier) over the installed payloads, plus
   `cp -a <installed package> <previous-package>` — the rollback source of §4.5.
2. Stop the service: `launchctl bootout gui/$(id -u)/org.mycelium.<service>` (`[NOT EXECUTED]`) or, to
   restart in place, `launchctl kickstart -k …` (`[NOT EXECUTED]`). Both are the same graceful path the
   runner implements as SIGTERM → worker process group → `stop_timeout_seconds` before SIGKILL
   (`[READ] mycelium_service_runner.py:98-114`: `os.killpg(…, SIGTERM)` at `:102`, the wait at `:106`, and
   `os.killpg(…, SIGKILL)` at `:111`, reached from the supervisor loop at `:188`; `[RAN] 08-runner-rehearsal.txt`:
   SIGTERM produced `state=stopped_by_manager`, `child_exit_code=-15`; a worker that ignored SIGTERM
   produced `-9`).
3. Build the new version **into the same recorded output root** (§2, fact 1) and verify the digests
   (§3.2).
4. Start it again and verify the running bytes (§4.4).
5. Keep the previous package until the new one has been up long enough to trust.

### 4.2 What an upgrade actually changes

`[RAN] 09-upgrade-rollback-rehearsal.txt`

With the config-only shape used there (a new `package_version` and one added argv flag), building into
the *same* output root changed exactly two files:

- `service-config.json` — **changed**, because the runner reads it at start;
- `service-package-manifest.json` — changed, because it records the payload digests.

`org.mycelium.<service>.plist` and `mycelium-<service>.service` were **byte-identical**, because the plist
embeds only the *path* of the config, not its contents. So a config/code upgrade does not require
re-bootstrapping the unit — but it does require restarting the process, since a running runner has
already read the old config. If the upgrade changes the config *path* (a new output root), all four files
change and the unit must be replaced and bootstrapped again.

### 4.3 Stop and start without losing state

`[READ] mycelium_service_runner.py:47-51,117-150,185-237`; `[RAN] 08`, `17`

- Durable state lives in the **state directory** (`/var/lib/mycelium/<role>`): the worker's own
  `identity` / `state.sqlite3` (seed), the membership state (node), and the runner's
  `.mycelium-service-<service_id>.json`. None of it lives in the package root.
- The graceful signal is `SIGTERM` (also `systemctl stop` / `launchctl bootout`). The runner forwards it
  to the worker's process group, waits `stop_timeout_seconds` (45 s node/seed, 90 s supervisor) and only
  then sends `SIGKILL`.
- Do not delete or move the state directory during an upgrade. Do not "clean" it to make a service
  restart — that also resets the restart budget without an audit trail (§4.6).

### 4.4 Verify the new bytes are the ones running

`[RAN] 17-running-bytes-and-wedge.txt`, `[RAN] 03-digest-verify.txt`

1. Installed payload digests equal the intended package's manifest digests (§3.2).
2. The unit's program is the runner pointed at that root's config:
   `ps -ww -o pid,ppid,lstart,command -p <pid>` shows
   `… -m mycelium_service_runner --config <OUTPUT-ROOT>/<role>/service-config.json`.
3. The worker's argv (`pgrep -P <runner pid>`, then `ps`) matches the `argv` **inside** that config file,
   and the config file's digest is the version you intended:
   `shasum -a 256 <OUTPUT-ROOT>/<role>/service-config.json`.
4. The process start time (`lstart`) is *after* the file's mtime: a process older than the file it is
   supposed to be running is still on the old bytes.

`[NOT EXECUTED - needs an installed unit]`: reading `runs`/`last exit code`/`pid` for a pilot label from
`launchctl print gui/$(id -u)/org.mycelium.<service>`; the command itself was executed on this host and
returned exit 113 because nothing is installed (`02-preflight-not-installed.txt`).

### 4.5 Rollback

`[RAN] 09-upgrade-rollback-rehearsal.txt` (step 5, `RESULT: PASS`)

Restore the previous payloads over the install root and re-verify every digest against the previous
manifest:

```sh
cp -a <previous-package>/. <OUTPUT-ROOT>/<role>/
shasum -a 256 <OUTPUT-ROOT>/<role>/*        # compare with the retained manifest digests
```

Then restart the service (§4.1 step 2). The package manifest names this procedure itself:
`"rollback": "restore_previous_digest_pinned_package"` in every `service-package-manifest.json`. If the
previous package was built at a different root, its plist bytes differ, so restore the unit too and
re-bootstrap it.

### 4.6 The trap: the restart budget survives a restart

`[RAN] 18-restart-budget-persistence.txt`

The budget is a window of start timestamps in the state file, reloaded on every supervisor start
(`[READ] mycelium_service_runner.py:134-150`; `restart_window_seconds=300`, `restart_limit=5` shipped).
Observed: after a crash-looping worker spent the budget, restarting the supervisor produced a supervisor
exit 0, `state=restart_budget_exhausted` and **zero** worker launches. Removing the state file made the
next start launch the worker again. So:

- a restart does **not** clear an exhausted budget; the service stays down even though launchd is happy;
- recovery is either waiting out `restart_window_seconds` (300 s) or, deliberately, removing
  `<state_directory>/.mycelium-service-<service_id>.json` and recording that you did;
- an upgrade during a crash loop can therefore look like "the upgrade did nothing" — check the state
  file before concluding anything about the new bytes.

---

## 5. Recovery

Each subsection gives a detection command and a remedy. Detection commands are read-only; none needs
root (`launchctl procinfo` does, and was refused — do not use it, `research/research-launchd.md` §3).

### 5.1 Crash-looping

**Detection.** `[RAN] 11-launchd-detection-surface.txt`

```sh
launchctl list | awk 'NR==1 || /org\.mycelium/'          # pid, last-exit-status, label
launchctl print gui/$(id -u)/org.mycelium.<service>      # state/runs/pid/last exit code  [NOT EXECUTED - nothing installed]
log show --predicate 'process == "launchd" AND eventMessage CONTAINS "org.mycelium"' --last 30m
```

A row with a **non-zero status column** is a crash loop — with a live pid it is looping right now, with
no pid it has stopped. The live demo shows both shapes on this host:
`02-preflight-not-installed.txt` (no pid, status 1 for `free-app`) and `19-demo-log-growth.txt`
(live pids `1170`/`1183`, status 1, for `free-app`/`paid-app`) — with the pid changing between the two
samples, which is the restart churn itself. The launchd log predicate prints
`service inactive: <label>` rows for each cycle; the command was executed here and returned only a
header, so **absence of rows is not proof of health** — it means the label was not cycled in the window.

**Remedy.** Read the exit code and the worker's own last line, then the state file:

```sh
tail -20 /var/log/mycelium/<service_id>.stderr.log
cat /var/lib/mycelium/<role>/.mycelium-service-<service_id>.json     # state + child_exit_code
```

- `state=restart_budget_exhausted` → §4.6 (wait the window out, or clear the state file deliberately).
- A traceback in the runner's stderr before any state file exists → the interpreter/module problem of
  §3.7; fix the package, not the host.
- `child_exit_code` non-zero with a named failure in the worker's stderr → fix the cause, then
  `launchctl kickstart -k` (`[NOT EXECUTED]`).
- Bound the log: launchd never rotates `StandardOutPath`/`StandardErrorPath` (`[READ] launchd.plist(5)`).
  Measured here today (`19-demo-log-growth.txt`): `mycelium-free-app.log` is **20,887,772 bytes
  (19.9 MiB)** in one inode born `2026-09-13 00:03:59` and still being written,
  `mycelium-paid-app.log` 3,601,125 bytes, and there are **no rotated siblings** in `~/Library/Logs`.
  The manifest's declared policy is an operator rotation of 10 MiB × 5
  (`"log_rotation": "platform_journal_or_10MiB_x5_operator_policy"`); the packager does not implement it,
  so it is this runbook's job to say an operator must.

### 5.2 A clean exit that launchd will not restart (the trap)

**The semantics, from the local man page.** `[RAN] 13-launchd-manpage-semantics.txt` quotes
`launchd.plist(5)`: `SuccessfulExit` *"If true, the job will be restarted as long as the program exits
and with an exit status of zero. If false, the job will be restarted in the inverse condition."* The
shipped plist sets `KeepAlive { SuccessfulExit: false }` (`04-plist-and-unit-keys.txt`), so launchd
restarts **crashes only**: a clean `exit(0)` is not restarted. The same man page records that
`NetworkState` and `TimeOut` are no longer implemented, so a plist relying on them silently does nothing.

**Why the product does this.** The runner returns 0 for its three deliberate end states —
`stopped_cleanly` (worker exited 0), `stopped_by_manager` (SIGTERM), `restart_budget_exhausted` — so
launchd stops relaunching once the runner has decided the service should be down
(`[RAN] 08-runner-rehearsal.txt`: three launches, then exit 0 with
`state=restart_budget_exhausted`).

**Detection.**

```sh
launchctl list | awk '/org\.mycelium/'        # a row with NO pid and status 0 = clean exit, not restarted
cat /var/lib/mycelium/<role>/.mycelium-service-<service_id>.json
```

`state` tells you which of the three it was. A row with no pid and status **0** is the trap: launchd is
idle by design, nothing is scheduled, and the service is *down*.

**Remedy.** Decide whether being down is correct, then act:

- correct (operator stop, or the runner's own budget) → leave it; record the state file as evidence;
- not correct → `launchctl kickstart -k gui/$(id -u)/org.mycelium.<service>` restarts it now
  (`[NOT EXECUTED]`). If the state file says `restart_budget_exhausted`, a kickstart alone will not
  launch the worker (§4.6).
- Because launchd will not do it, "the node silently stays down" is the failure mode to watch for; a
  watchdog that alerts on a missing pid is the mitigation, and it does not exist yet (§7).

### 5.3 A wedged worker that is alive but not progressing

**Detection.** `[RAN] 17-running-bytes-and-wedge.txt`

```
worker alive?                    : True
TCP connect to 8876              : True     <- the port still accepts
health GET, 2 s deadline         : TimeoutError   <- the only local signal that fires
runner state file                : running (unchanged, still 'running')
state file observed_at advanced? : False
```

The rehearsal wedged a *real* worker with `SIGSTOP` and measured exactly this: liveness and the port
probe stay positive, the health request with a deadline fails, the runner's state file is still `running`
and its `observed_at` no longer advances. So:

```sh
curl -sS -m 2 -o /dev/null -w '%{http_code}\n' <health_url>   # 200 = answering; a timeout = wedged
ps -p <worker pid> -o pid,stat,lstart,command                 # 'T' in stat is a stopped process
pgrep -P <runner pid>
```

Do **not** diagnose a wedge from stdout: a healthy worker is silent (§3.6), so a frozen log means
nothing. Do **not** treat "port accepts" as health: a wedged worker still accepts.

**Remedy.** `launchctl kickstart -k` / `SIGTERM` to the runner (`[NOT EXECUTED]`; the SIGTERM path itself
was rehearsed: `state=stopped_by_manager`, `child_exit_code` -15 for a cooperative worker and -9 when the
worker ignored SIGTERM, `[RAN] 08`). Capture `ps` and the health probe output *before* restarting, because
the wedge evaporates with the process.

### 5.4 Sleep/wake or thermal throttling stalling a deadline

**Detection.** `[RAN] 10-power-thermal-detection.txt`

```sh
sysctl -n kern.sleeptime kern.waketime kern.boottime
uptime
pmset -g assertions
timeout 20 pmset -g therm        # bound it: some pmset -g forms are live streams
pmset -g uuid
timeout 60 pmset -g log | tail -3
```

Measured here: `kern.sleeptime = { sec = 0 }` and `kern.waketime = { sec = 0 }` (boot
`Tue Sep 15 09:14:27 2026`), `uptime 6:54`, `pmset -g uuid` = `(NULL)`, and
`pmset -g therm` reporting only *"No thermal warning level has been recorded"*. Those are **negative
observations about the retained window, not a measurement**: the durable signal is the `kern.sleeptime`
delta, and `pmset -g log`'s history only reaches back as far as it retains. `pmset -g assertions` shows
`UserIsActive 1`, `PreventUserIdleSystemSleep 1`, `PreventUserIdleDisplaySleep 1`,
`PreventSystemSleep 1` — some process (an unmanaged `caffeinate` on this host) is asserting, so this host
has not slept since boot.

**Remedy.**

- Attribute a stall by the delta, not by a missing log row: if `kern.sleeptime` advanced across the
  stall, the deadline was crossed by a sleep and every wall-clock-derived bound is suspect for that
  window. `caffeinate` is not durability — `research/research-launchd.md` §4.9 records the repo's own
  runbook saying so, and §4.8 that `caffeinate -u` expires after 5 s without `-t`.
- For a soak or a deadline-bearing run, run with assertions held and re-check `pmset -g assertions`
  after the fact; `[NOT EXECUTED - writes to pmset (disablesleep) are forbidden here]` is the closed-lid
  path, which needs operator approval on the target host.
- Do not "fix" a stalled deadline by widening a constant: `maximum_total_cleanup_ms` and the route
  deadlines are pinned by a contract and by the UI (§5.6).

### 5.5 A stuck or duplicate control frame

**Detection.** `[RAN] 12-soak-trace-evidence.txt`

The lifecycle traces are written by `mycelium_testbed.soak`, one run directory per seed:

```sh
cd ~/Documents/playground/mycelium-core-pilot
/opt/homebrew/bin/python3 -B -m mycelium_testbed.soak --seed <N> --events 200 \
  --run-id <run-id> --out <runs-root>
# traces land in  <runs-root>/<run-id>/seeds/seed-<NNNN>/mailbox/{run.json,requests/,commands/}
# summary + timeline for one run directory:
/opt/homebrew/bin/python3 -B -m mycelium_testbed <runs-root>/<run-id>/seeds/seed-0003/mailbox
```

The trace header (`run.json`) carries the protocol (`mycelium.testbed.trace.v1`), the git head, the host
ids and a `package_digest`, so a trace is bound to the bytes that produced it. The summariser prints the
clock-offset verdict (in the mailbox rehearsal: `no paired exchange found … hosts are NOT interleaved`,
i.e. the two hosts are *not* on one timeline — do not read cross-host ordering out of such a run).

Duplicated control frames are visible as their own events:

```sh
grep -h '"kind":"delivered"' <trace>/requests/*.jsonl | grep -c '"status":"duplicate"'
grep -h '"kind":"delivered"' <trace>/requests/*.jsonl | grep '"status":"duplicate"' | head
```

The rehearsal run contained 18 such events. This is the observable behind
`rca/wp2-duplicate-frame-observation.md`: over that 20-seed sweep, 6 of 20 seeds delivered a duplicated
iroh frame, every failing seed had one present, and the request failed closed with a named terminal
(`transport_closed`) inside budget. At that **default** injected duplicate rate the invariant held and the
cost was liveness, not execution safety.

**Correction (2026-09-16, packet a4-at-most-once) — the at-most-once invariant is not only about
duplicated frames.** `rca/wp2-8-at-most-once-duplicate-frame.md` first reported a firing of
`at_most_one_native_execution_per_command` at 5× the default duplicate rate, and then corrected its own
mechanism hypothesis in a dated "Correction" section. The measured cause is **not** a duplicated frame
reaching execution:

- the firing episode had **no duplicated frame at all** — `SoakTransport`, the only transport-fault
  consumer, is installed for the canonical episode only;
- the two native executions came from two different call paths (`receive_hop` → `_execute_runtime`, then
  `receive_progressive_prefill` → `_execute_runtime`), captured in-process with both call stacks;
- the real mechanism is that **a dispatch rejected after its enqueue leaves its `HopWorkItem` in the
  relay's shared scheduler queue**, and a later, unrelated dispatch's `pop_next()` pops and executes it —
  a second native execution of an identity whose path had already been released.

So the earlier "dedupe by message id" direction (and the same direction in `packets/wp5-preconditions.md`
§7) pointed at the wrong layer. The fix that landed is in the scheduler/queue path:
`5371acd fix(router): never execute a work item whose dispatch was rejected`, gated deterministically (it
fires in ~0.07 s on **every** run) by
`tests/path_cancellation_adversarial/test_at_most_once_dispatch.py` — re-run while reviewing this file,
`4 passed in 0.08s` (`runs/20260921-closure-train/wp9/03-at-most-once-gate.txt`). The soak is corroboration
for this defect from now on, not the detector.

**Calibration warning.** Do not use `grep -L '"kind":"terminal"' <trace>/requests/*.jsonl` as a
stuck-request detector: in the green rehearsal run (soak's own `green=True`, no invariant codes) that
filter flagged 7 of 17 request files, all of them hash-suffixed replica ids. The authoritative signals are
the soak's invariant codes — `request_without_terminal_frame_within_budget`,
`nonterminal_row_not_retired_within_bound`, `at_most_one_native_execution_per_command`,
`control_loop_death_while_membership_valid` — and the `terminal` events of the canonical request id you
submitted.

**Remedy.**

- A stuck frame with a live worker: restart the worker (§5.3); the request is expected to reach a named
  terminal inside budget, so a hang past budget is the defect to report, not to retry silently.
- A duplicated frame: the *tolerance* decision is **open and WP5-owned** (§7) — either dedupe the
  receipt/pump path by message id, or accept fail-closed-with-a-name as the contract and record it in the
  claim sheet's exclusions. Do not weaken `at_most_one_native_execution_per_command` while doing it. Note
  that this is now a *scoping* question, not the root cause of the at-most-once firing: that was the
  scheduler-queue defect corrected above and fixed in `5371acd`.
- Reproduce deterministically before and after any change:
  `python3 -B -m mycelium_testbed.soak --seed <N> --events 200 --run-id repro`. Read the result
  as **structural**: what gates is any invariant firing and terminal coverage, named in
  `replay_check.gate_failures` (and reflected in the soak's exit code). The replay digests are
  *recorded observations*, not gates — a fixed `--events` budget covers a variable number of
  request lifecycles, so product-digest identity held 5/20 canonical runs and event-stream
  identity 29/30 (`rca/wp2-harness-assertion-design-review.md` decision 2', revised 2026-09-15).
  `12-soak-trace-evidence.txt` records an identical event-stream digest for its replayed seed;
  do not read that as a pass/fail criterion.

### 5.6 A stale qualification value or advertised bound

`[RAN] 14-stale-bound-detection.txt`. The defect shape (`packets/wp5-preconditions.md` §8) is *a readiness
or authority value captured once at start-up and never re-derived while its input can change*. Local
handles:

```sh
grep -n "maximum_total_cleanup_ms" physical_inference_node.py            # :2202 -> 2_000, a constant where a measurement belongs
grep -n "maximum_total_cleanup_ms\|2_000" ui/web/src/features/liveRoute/routeStatus.ts
#   :69, :485, :490, :500, :564 -> the UI pins the same value, so changing it is a contract migration
grep -n "self.endpoint_addr = ready" physical_inference_node.py         # :1637, republished at :1651
grep -rn "address_changed" native/iroh_transport/src/ready_address.rs    # :162-213 the fix emits it
grep -rn "address_changed" --include=*.py .                              # no Python consumer (WP5 gap)
grep -n "advertise" mycelium_node/__main__.py mycelium_node/membership.py mycelium_seed/coordinator.py
```

**Detection for an operator.** A published address or bound is stale if it is not re-derived when its
input changes: the operator-visible check today is to compare the value the service advertises against the
value measured at the same moment (`endpoint_addr` in a projection vs `mycelium_iroh_sidecar`'s current
address; the advertised cleanup bound vs the queue/delivery/execution terms in a trace).
`[NOT EXECUTED - needs running services and the WP5-side re-derivation; the local greps are all that can
be exercised on this branch]`.

**Remedy.** None on this branch, by design: `packets/wp5-preconditions.md` §8 assigns the fix (re-derive
or reject with a named reason) to WP5, and the criterion-4 trial cannot pass until the node/membership
snapshots are re-derived. An operator must **not** hand-edit the constant: the UI pins it
(`routeStatus.ts:490`,`:564`), so a change is a contract migration through the canonical producers, and
`docs`/manifest rules forbid hand edits. Until WP5 lands, treat "the peer kept advertising its start-up
address" as an expected symptom of a network change, not as a bug in the peer.

One half of that delivery-path work **has** landed, so do not look for it as a pending item: the owner's
cleanup-receipt wait is event-driven now — `[READ] mycelium_live/route.py:3185` waits on the route's stop
event with the poll interval as its upper bound, rather than sleeping out a bare interval. What is still
missing is the same section's other half: re-deriving the advertised value instead of publishing one
captured at start-up.

### 5.7 Quick reference: the runner's own status vocabulary

`[RAN] 08-runner-rehearsal.txt`, `[READ] mycelium_service_runner.py:74-95`

| `state` in the state file | Meaning | launchd behaviour |
|---|---|---|
| `starting` | attempt recorded, worker not spawned yet | still running |
| `running` | worker process alive | still running |
| `restart_pending` | worker died non-zero, waiting `restart_delay_seconds` | still running |
| `stopped_cleanly` | worker exited 0 → runner exits 0 | **not restarted** |
| `stopped_by_manager` | SIGTERM handled, worker terminated → exit 0 | **not restarted** |
| `restart_budget_exhausted` | `restart_limit` starts inside `restart_window_seconds` → exit 0 | **not restarted** |

Exit codes: 0 for all six states; 2 when the config is invalid
(`[RAN] 08`: an incomplete `service-config.json` → exit 2, `ServicePackageError`, no child spawned).

---

## 6. Evidence to collect after an incident

Collect these before restarting anything — a restart destroys the process-level evidence.

1. **The gate record**: `runs/wp2-final-gate/PACKAGE.json` (package digest, git head, per-file digests,
   source mode, working-tree cleanliness) plus the package tree. Compare the installed tree against it
   (§3.2).
2. **The lifecycle traces**: the run directory written by `mycelium_testbed.soak` (§5.5) —
   `<runs-root>/<run-id>/seeds/seed-<NNNN>/{run.json,requests/,commands/}` plus `report.txt` and
   `sweep.json`. The header binds the run to a git head and a `package_digest`; the `terminal` events and
   the invariant codes are the verdict.
3. **launchd state dumps**:
   ```sh
   launchctl print gui/$(id -u)/org.mycelium.<service>   # state, pid, runs, last exit code, minimum runtime
   launchctl list  | awk 'NR==1 || /org\.mycelium/'      # pid, last exit status, label
   launchctl print-disabled gui/$(id -u) | grep -i mycelium
   log show --predicate 'process == "launchd" AND eventMessage CONTAINS "org.mycelium"' --last 1h
   ```
   (`launchctl print` output is not API — `[READ] launchctl(1)` — so record the whole dump and the
   macOS build, `26.5.1 (25F80)` here.)
4. **The runner's state file**: `/var/lib/mycelium/<role>/.mycelium-service-<service_id>.json` — state,
   `attempt_count_in_window`, `restart_limit`, `restart_window_seconds`, `child_exit_code`,
   `observed_at_unix_ms`, and the list of start timestamps.
5. **Logs**: `/var/log/mycelium/<service_id>.stdout.log` and `.stderr.log` (unrotated by launchd —
   §5.1). Save the tail, not the whole file; the demo's equivalent reached 19.4 MiB.
6. **Power/clock context**: `sysctl -n kern.sleeptime kern.waketime kern.boottime`, `uptime`,
   `pmset -g assertions`, `pmset -g therm`, `timeout 60 pmset -g log | tail -50`.
7. **Process context**: `ps -ww -o pid,ppid,lstart,command -p <runner pid> <worker pid>` and
   `pgrep -P <runner pid>` — this is how a later reader tells which bytes were running (§4.4).

---

## 7. Pending WP5 (do not read any of this as existing)

None of the following is implemented on this branch; the runbook will need a section for each when it
lands. The pilot-specific material belongs to WP5 and is deliberately **not** described here as done.

- **The A13 adapter / pilot origin / hostnames and auth.** The services here are bound to
  `127.0.0.1` only (`seed.json`, `supervisor.json`). Nothing in this runbook covers a public origin, a
  tunnel, a service token, or Cloudflare Access. `packets/wp6-hostnames.md` and
  `packets/wp5-preconditions.md` §6 hold the (unimplemented) requirements.
- **Bringing the node up at all.** `[RAN] 15-install-shape-rehearsal.txt` step 6 runs the shipped node
  argv with a proper scratch data dir: exit 2, `node_preflight_failed`. The likely missing pieces are the
  enrollment bundle (first enrollment needs a join bundle — `[READ] mycelium_node/__main__.py:440-460`)
  and the iroh sidecar at `/opt/mycelium/current/bin/mycelium-iroh-sidecar`; I did not isolate which,
  so this runbook claims only "it does not come up today".
- **The supervisor's live mode.** `mycelium_demo serve --mode live` needs
  `/var/lib/mycelium/operator-plan.json` and the built UI at `/opt/mycelium/current/ui/web/dist`.
  `[NOT EXECUTED - would bind 8791 and needs WP5 material]`.
- **Useful-model stage packs / the model artefact.** Nothing here covers model bytes, MLX, or stage
  packs.
- **Duplicate-frame policy** (§5.5): the *tolerance* decision (dedupe vs fail-closed-with-a-name) is still
  open. The execution-safety half is **closed**: the at-most-once firing was a scheduler-queue defect
  (a rejected dispatch left its `HopWorkItem` for a later `pop_next()`), fixed in `5371acd` and gated by
  `tests/path_cancellation_adversarial/test_at_most_once_dispatch.py`. The message-id-dedupe direction
  originally recorded here did not describe that firing
  (`rca/wp2-8-at-most-once-duplicate-frame.md`, "Correction (2026-09-16)").
- **Stale advertised bound / address re-derivation** (§5.6): the invariant "a published address or bound
  is either re-derived on change or rejected with a named reason" and one test per site.
- **Rotation and watchdog.** The manifest declares
  `"log_rotation": "platform_journal_or_10MiB_x5_operator_policy"` but no rotation exists, and nothing
  watches for the "clean exit, not restarted" state of §5.2.
- **The C6 exercises.** `packets/wp5-c6-exercises.md` specifies six harness exercises (clean-exit
  recovery, crash restart, sleep/wake, thermal/App Nap, cancellation + restart, log growth) with named
  outcomes. This runbook's §5 is the operator-side sibling of them; it is not a substitute, and the
  exercises have not been claimed here.
- **Qualification / readiness records** for a node on this host: out of scope here, and no such record is
  asserted anywhere in this document.

---

## 8. Open risks

1. **The shipped packages crash-loop on any host whose `python3` is below 3.10** (§3.7), and this host's
   `/usr/bin/python3` is 3.9.6. Until `release/service-configs/*.json` pins an interpreter ≥ 3.10 and the
   packages are rebuilt and re-recorded, "install" as shipped cannot succeed. This is a config decision,
   not a runbook workaround.
2. **The shipped configs target root-owned paths** (`/opt/mycelium/current`, `/var/lib/mycelium/*`,
   `/var/log/mycelium`) that do not exist here, so the install path cannot be completed without root —
   and the per-user agent plist would run with a `PYTHONPATH` pointing at a directory that does not
   exist. Nothing in this runbook was able to exercise the real paths; only scratch equivalents were.
3. **`ThrottleInterval` is 5 s and launchd has no give-up for crashes.** A crash-looping unit is a
   self-sustaining 5-second loop with an unrotated log (§5.1); the runner's budget only helps once the
   runner itself starts.
4. **The restart budget persists** (§4.6), so "restarted the service" and "the service is running" are
   different statements; a naive health check on the unit will read green while the worker is never
   launched.
5. **Evidence is host-bound.** `launchctl print` output is explicitly not API, `runs` is reset by
   bootstrap/boot, and the sleep/thermal detectors were exercised but never fired (§5.4). Any claim built
   on them is only as good as the recorded macOS build.
6. **The soak traces used here are harness-side** (`mycelium_testbed/DESIGN_NOTES.md` records that the
   trace subscribes to existing harness seams), so a *physical* run's timeline still needs the WP5/WP8
   per-host emission decision — the clock-offset verdict in `12-soak-trace-evidence.txt` shows the
   limitation concretely.
7. **This document was written against the packages built at `a5437e2…`.** Re-run §3.2 before relying on
   any digest in it.
8. **`tests/security/test_release_security_audit.py::test_current_checkout_and_runbook_preserve_claim_boundary`
   was RED at frozen head `e23d243`, and was not caused by this document.** It failed on
   `audit_repository(ROOT)["ok"] is True` with one finding:
   `secret_cli_argument` at `mycelium_testbed/emulation.py:4020`, subject `--token-steps`. That is the
   emulation's token-*step* budget (`TOKEN_STEPS = 8`, `mycelium_testbed/emulation.py:204`), not a secret;
   the audit's own rule flags any `--<secret-word>` option that does not end in a file/env/fd/path/ref
   suffix (`scripts/release_security_audit.py:66-69,240-243`), so the name trips it as a false positive.
   This document declares no CLI argument at all (0 hits for `add_argument`), so it cannot produce that
   class of finding; the failing byte is a harness source file, and neither renaming its option nor
   relaxing the audit rule is a docs-only change. Left untouched on purpose — see
   `runs/20260921-closure-train/wp9/06-claim-boundary-attribution.txt`. The document half of the same test
   (`docs/security/release-security-audit.md`'s boundary literals) is unaffected by this file.

   **Source repair:** `2c1115a` renames the emulation-only CLI option to `--generation-steps`
   while preserving the integer parser, default and runtime `token_steps` parameter. The old spelling
   is no longer accepted; update external emulation commands accordingly. No security-audit rule,
   exception or guard changed. The repair's CLI/emulation/security/claims/contracts selection passed
   94 tests (`runs/20260921-closure-train/security-fix/green.log`); that is not full-suite or physical proof.
