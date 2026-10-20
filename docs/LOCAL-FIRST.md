# Running Mycelium locally (local-first runbook)

Goal: your own seed, nodes and UI on your own network, with no `mycelium.now`,
no hosted directory and no third-party relay. This page says what works **today**,
what the new `--network-mode lan` adds in this lane, and what is **not possible
yet**. Labels used below:

- **TESTED (unit)** - covered by tests in `tests/network_mode/` and `tests/a13_macos/test_app_relay_order.py`; no real network connection was made.
- **SOURCE-READ** - read in the code, not executed.
- **UNVERIFIED** - not run on hardware. Treat as a plan, not a result.

Nothing on this page has been run on two physical machines. Do not read it as a
qualification claim.

## 1. Network modes

One closed setting, `internet` (default) | `lan` | `local`, chosen with
`--network-mode <mode>` or `MYCELIUM_NETWORK_MODE=<mode>` (the flag wins).
Unknown values are rejected, never guessed. TESTED (unit).

| mode | meaning | what contacts hosted services |
|---|---|---|
| `internet` | **Today's behaviour, unchanged.** iroh N0 preset, public HTTPS origins. | n0 relays, n0 pkarr/DNS (sidecar); Cloudflare tunnel (A13 app) |
| `lan` | Private network. No relay, no n0 lookup, RFC 1918 addresses, private directory/entry origins accepted. | nothing, by design (see limits in section 3) |
| `local` | Loopback only (the sidecar `--local-only` test mode). | nothing |

With no flag and no environment variable every command produces exactly the
arguments it produced before this lane (TESTED (unit): `test_node_internet_runtime_network_is_exactly_todays_arguments`,
`test_seed_internet_default_is_todays_arguments`).

## 2. What `--network-mode lan` does in this lane

| component | effect | status |
|---|---|---|
| `python -m mycelium_seed` | `--bind` defaults to the first RFC 1918 address of the primary route (a UDP route lookup; no packet is sent). Wildcard bind derives `--advertised-url http://<that address>:<port>`. Refuses `--trusted-https-proxy`, loopback or public binds, and public advertised addresses. | TESTED (unit); the bind itself UNVERIFIED |
| `python -m mycelium_node` | `--advertise` becomes optional and defaults to `http://<private address>:8766` (`--advertise-port` changes the port). Fails closed if the host has no private address. `internet` still requires `--advertise`, with the same failure output as before. | TESTED (unit) |
| node, stage-runtime peer | **Refused at preflight**: the sidecar has no LAN mode yet (section 3). Membership-only peers (`--membership-control-only`, source-only classes) are accepted. | TESTED (unit) |
| `physical_inference_node.py` | New `--sidecar-lan` (mutually exclusive with `--sidecar-local-only`/`--sidecar-force-relay`); forwards `--lan` to the sidecar. The sidecar does not know `--lan` yet. | TESTED (unit) |
| `mycelium_swarm_directory` | `--network-mode lan` lets the directory accept `http(s)://<RFC 1918 address>[:port]` entry origins. | TESTED (unit) |
| A13 `SwarmPublisher` | `network_mode=NetworkMode.LAN` accepts a private directory origin and a private entry origin. The default is `internet`; the A13 app does not pass `lan` yet. | TESTED (unit) |

### Offline model source

`--offline` (or `MYCELIUM_OFFLINE=1`) means Hugging Face is never contacted.
`--network-mode lan` and `local` imply it; `--no-offline` or `MYCELIUM_OFFLINE=0`
overrides that, and the CLI flag beats the environment, which beats the
implied default. With nothing set in `internet` mode the behaviour is exactly
the old one (TESTED (unit): default paths still download). `--offline` is accepted
by `python -m mycelium_node`, `python -m mycelium_seed`, `python -m mycelium_demo serve`
(`--network-mode` is accepted there too, only to imply the switch) and the A13
launcher (`python -m mycelium_a13_macos`); they export the resolved value so the code
below sees it. `serve` does not forward either flag to the live supervisor; the
supervisor reads the exported value. TESTED (unit).

Every Hugging Face site is guarded, and a refusal names the local/LAN source to use
(error code `offline_model_source_required`):

| site | offline behaviour |
|---|---|
| `mycelium_a13_macos/acquisition.py` (`_HTTPRepositoryTransport`, metadata and ranged/streamed bytes) | refuses the public origin before any request; a private mirror passed as `api_origin`/`download_origin` is still allowed |
| `mycelium_a13_macos/peer_model_source.py` (`joiner_model_source`, `MultiSourceTransport`) | no Hugging Face source is built: owner LAN, then owner relay only |
| `weight_provisioning.py` (`fetch_huggingface_file`) | reads the local Hugging Face cache only; a missing file fails closed |
| `model_manifest.py` (`resolve_huggingface_manifest`) | refused (it needs the Hub) |

TESTED (unit, every network opener replaced by one that fails the test). Not
covered: scripts under `scripts/` and the owner-side build tools that call the
library directly, other than through the guarded functions above.

### Fully offline model transfer (owner to member)

What works offline, TESTED (the real owner `DemoApp` managed-control plane, a real
member and `SelectedModelAcquisition` in one process, with the owner's LAN HTTPS
listener on loopback; no second machine, no hardware):

- The owner serves its verified, pinned manifest (file list, sizes, digests, model id
  and model commit) to live members on a second signed route,
  `/managed-control/model-source/manifest`, next to the byte-range route. It uses the
  same checks as a byte read: a member statement signed with the member's key and
  checked against the current member record, a still-live member, replay refusal, and
  the exact pinned model and commit. It is served on the relay path and on the
  owner's LAN-direct HTTPS listener (TLS pinned to the certificate in the owner-signed
  announcement).
- With `--offline` (or `lan`/`local`) a member's acquisition takes the file list from
  the owner instead of from Hugging Face metadata, then downloads and hash-verifies
  every file from the owner as before. A manifest for another model or another commit
  is refused with `model_manifest_commit_mismatch` before any byte is requested; a
  malformed one with `model_manifest_invalid`. Online behaviour is unchanged
  (Hugging Face metadata first).
- Trust, stated plainly: offline, the digests come from the owner (signed, and bound to
  the pinned model id and commit), not from Hugging Face. Every received byte is
  checked against them, so a damaged or tampered file fails. A malicious owner could
  serve a self-consistent but different file set under the pinned commit and the
  member could not detect it offline, because the commit is not recomputed from the
  files. The owner is already the swarm authority, so this adds no new party, but it is
  weaker than checking against Hugging Face.

What this does not give you yet: the A13 multi-device flow still reaches the owner's
control plane through the Cloudflare relay (section 3), and inference across machines
still needs the Rust LanDirect sidecar. So today "fully offline" holds for the model
transfer logic, not for an end-to-end two-Mac run.

### Model from a local folder

```sh
# once, on a machine that is online (writes a new file, never overwrites):
python -m mycelium_demo models fetch-manifest --model <id> --commit <40-hex> --out pinned.manifest.json
# on the offline machine, with the model files in a folder (USB stick, shared drive):
python -m mycelium_demo models import <folder> --model <id> --commit <40-hex> \
  --manifest pinned.manifest.json --cache-root "<app state folder>/model-source"
```

The import checks every file in the folder against the pinned manifest first (exact
set of files, exact sizes, exact SHA-256 or git blob id), refuses the whole import if
anything is missing, unexpected, a link, the wrong size or the wrong content, and lists
every offending file in plain English. Nothing is written to the cache on a refusal.
Otherwise the files are copied (hashed again while copying), installed read-only
(mode 0400) in the layout the app's own download uses, so the next download of that
model finds it and fetches nothing. It refuses to replace a model that is already in
the cache. Only registered models are accepted. Offline (`--offline`, or `lan`/`local`)
it needs `--manifest`; it never contacts Hugging Face. `--cache-root` must be the
app's `model-source` folder (a non-default run model uses
`model-source-<first 12 of the commit>`; a target uses `serving/model-source...`
under its own state folder). There is no picker in the app UI. TESTED (unit,
temporary folders only).

### `doctor --lan`

`python -m mycelium_demo doctor --lan` (no `--state-dir` needed) prints one line
per check and, for each failing one, a plain-English `Fix:` line. Exit status 0
means ready, 1 means not. `--json` prints the report, `--network-mode`,
`--offline/--no-offline`, `--port` (repeatable; default 8765 and 8766) and
`--origin URL` (repeatable, an origin you plan to use) adjust it.

1. network mode is `lan`;
2. a private (RFC 1918) address was found for this device;
3. the ports are free (a loopback-only bind test; nothing listens on the LAN);
4. nothing hosted is configured: no `trycloudflare.com`, `mycelium.now`, n0
   relay/DNS, `--force-relay`/`--relay-url`, or public HTTPS origin in `MYCELIUM_*`,
   `IROH_*`, `HF_ENDPOINT` environment variables or `--origin`;
5. the offline switch is on.

Names of variables and flags are reported, never their values. TESTED (unit,
probes injected); the default probes were run once on a development Mac. The
doctor does not detect Wi-Fi client isolation or firewall blocks (see section 7).

Private-origin rule (swarm directory/entry): exactly `http` or `https`, a
canonical IPv4 literal in 10/8, 172.16/12 or 192.168/16, optional port, no path,
no user info. Hostnames, public addresses, link-local, loopback and IPv6 are
rejected; the default policy (public HTTPS only, loopback behind its own flag)
is unchanged.

## 3. What is not possible yet

- **Inference across two machines without the internet.** The iroh sidecar only
  has the N0 mode (relay + n0 DNS) and a loopback-only `--local-only` mode. A
  relay-less LAN bind (`LanDirect`) is **design only**:
  `docs/design/lan-direct-transport.md`. Until it lands, `lan` mode starts the
  seed and membership, not the stage runtime.
- **The A13 desktop app's multi-device flow.** Its only reachability path is a
  Cloudflare quick tunnel and join codes are hard-wired to `*.trycloudflare.com`
  (SOURCE-READ: `mycelium_a13_macos/app.py`, `pairing.py`). Do not start the
  "public relay" if you want zero hosted services; the single-machine path works
  without it.
- **Relay order: no longer a trap.** The relay can start before or after the swarm is created.
  Creating with the "Let other Macs join" box still starts the relay first. For a swarm
  created without one, `/api/network/start` reopens the retained authority on the relay's
  origin (`endpoint_recovery.attach_public_endpoint`; same swarm identity, same member), and a
  failure closes the relay and restores the swarm as it was. Join codes can be asked for at
  any time: before the relay exists they are kept as "ready to share once the relay starts" and
  open as real codes when it starts. Serving (`/api/demo/start`, `/api/serving/start`) with
  joined members and no relay answers `relay_required` with `fix: start_relay`.
- **Route bring-up over LAN.** `mycelium_live/route.py` forwards only
  `--sidecar-force-relay`; the operator plans use `ssh_target`.
- **Discovery.** There is no mDNS/Bonjour. Peers learn each other's addresses from
  the membership exchange; you pass or auto-derive the seed address.
- **A fully offline two-device run.** The model transfer no longer needs Hugging Face
  (section 2: the owner serves the pinned manifest and the bytes, and a folder can be
  imported), but the rest of the A13 flow still does not run without hosted
  services: reaching the owner's control plane uses the Cloudflare relay, and the
  stage runtime needs the Rust LanDirect sidecar (design only). Nothing in this
  lane has been run on two machines. The app UI has no "model from a folder" picker;
  use the command above.
- **Transport security of the seed.** The seed endpoint is plain `http`. Use it
  only on a network you control; invites are single-use and signed but the
  channel is not encrypted. TLS for LAN origins is not implemented.

## 4. Privacy

SOURCE-READ across product code: no telemetry, analytics, crash reporting,
update check, NTP, external CDN or font. The things that can leave your network
are: n0 relays/DNS (`internet` sidecar), Cloudflare (A13 public relay and its
DoH fallback), Hugging Face (model downloads, off with `--offline` and in
`lan`/`local` mode), and package indexes at install time. In `lan`/`local` mode
none of these is configured by the commands below, but until the sidecar LAN
mode exists that is only true for membership. `doctor --lan` checks the
configuration side of this.

## 5. Runbook: a private seed and a membership-only node on one LAN

All commands UNVERIFIED on hardware. Python 3.14 is what the repo and CI use.
Use whichever `python` your OS provides, via `python -m`, rather than a
hard-coded path. State directories must be owner-only (`chmod 700` on macOS and
Linux; the Windows installer applies ACLs).

On each machine, first:

```sh
export MYCELIUM_NETWORK_MODE=lan                      # or pass --network-mode lan
python -m mycelium_demo doctor --lan                   # fix every line that says [fix]
```

On the seed machine:

```sh
mkdir -m 700 "$HOME/mycelium-seed"
python -m mycelium_seed --data-dir "$HOME/mycelium-seed" --swarm-id my-lan --seed-node-id seed-node --init-only
python -m mycelium_seed --data-dir "$HOME/mycelium-seed" --swarm-id my-lan --seed-node-id seed-node --port 8765
```

The second command binds your private address and prints the advertised URL as
its `seed_started` status line. Check it is the interface you intend; if the host
has several, pass `--bind <ip>` explicitly.

Mint one invite per device (single use, 15 minutes by default):

```sh
python scripts/mint_swarm_invites.py --seed-data-dir "$HOME/mycelium-seed" \
  --seed-url http://<seed-private-ip>:8765 --swarm-id my-lan \
  --output-root "$HOME/mycelium-invites" --count 1 --ttl-seconds 900
```

Deliver the file over a channel you trust (USB stick, AirDrop). Do not paste it
into chat or a command line.

On each other machine:

```sh
python -m mycelium_node --network-mode lan --data-dir "$HOME/mycelium-node" \
  --join-bundle-file /path/to/native-node-001.invite.json \
  --node-id my-laptop --membership-control-only
```

`--advertise` is omitted on purpose: it defaults to this machine's private
address. Expected today: the node joins and renews its lease against the seed.
Not expected today: any inference traffic (section 3).

Single machine, no network at all (already supported, SOURCE-READ):
`python two_process_runtime_qualification.py --json` from the README
"Verification" section, and the sidecar `--local-only` mode.

## 6. Installing on macOS, Windows and Linux

Source: the per-user self-installers `mycelium_a13_macos/installer.py`,
`mycelium_a13_windows/installer.py`, `mycelium_a13_linux/installer.py` and the
build scripts `scripts/finalize_a13_macos.py`, `scripts/build_a13_windows.py`,
`scripts/build_a13_linux.py`. All three verify an Ed25519-signed manifest and
the payload digest before writing anything, install per user without
administrator rights, and never register an always-on service without a
separate consent. Packages are produced as owner-private media: there is no
public download. **All steps in this section are UNVERIFIED on hardware**; the
Windows and Linux installers have only run against test seams.

### macOS (Apple silicon only, macOS arm64)

1. Obtain `mycelium-<version>-macos-arm64.zip` from the owner and compare its SHA-256
   with the value the owner sent.
2. Extract it (Archive Utility or `ditto -x -k <zip> <dir>`).
3. Run `Install Mycelium.command` from the bundle. The app is unsigned and not
   notarized, so Gatekeeper asks on first launch (right-click, Open). The
   installer keeps the quarantine attribute on purpose.
4. Result: program in `~/Library/Application Support/Mycelium/programs/<version>`,
   link `~/Applications/Mycelium A13.app`, state in
   `~/Library/Application Support/Mycelium A13 macOS Demo`.
5. Allow incoming connections when the macOS firewall prompts for the sidecar
   or Python. Intel Macs are refused (MLX only).
6. From source instead: Python 3.14, the repo checkout, and a built sidecar
   (`cargo build --release` in `native/iroh_transport`; needs the Rust
   toolchain and, the first time, crate downloads).

### Windows (10 build 19045 or newer, x86_64)

1. Obtain the zip from the owner; compare its SHA-256 (the first install pins the
   publisher key on first use; there is no Authenticode certificate, so
   SmartScreen will warn).
2. Extract, run `Install Mycelium.cmd`. It runs `Mycelium\Mycelium.exe --install`.
3. Result: `%LOCALAPPDATA%\Programs\Mycelium\<version>`, Start-menu shortcut,
   Apps & features entry, state in `%LOCALAPPDATA%\Mycelium\A13`.
4. Allow the sidecar through Windows Defender Firewall (inbound UDP) when prompted
   (UNVERIFIED which rules are needed).
5. Compute is NumPy float32 on CPU (small models only). The unattended task is a
   separate command: `python -m mycelium_a13_windows.service_install`.
6. Device bring-up helper: `scripts/a13_windows_device_bringup.ps1` (portable-source
   gates and a hardware survey; it installs nothing).

### Linux (Ubuntu 24.04 or newer, x86_64 or aarch64, systemd)

1. Obtain `mycelium-<version>-linux-<arch>.tar.gz` from the owner; compare its SHA-256.
2. Extract and run `./install.sh` (it runs `mycelium/mycelium --install --bundle <bundle>`).
3. Result: `${XDG_DATA_HOME:-~/.local/share}/mycelium/<version>`, launcher
   `~/.local/bin/mycelium`, desktop entry, state in
   `${XDG_STATE_HOME:-~/.local/state}/mycelium/A13`. Make sure `~/.local/bin` is on `PATH`.
4. Other distributions are refused until admission is widened. Open the firewall
   (`ufw allow` for the sidecar's UDP port) if `ufw` is active.
5. Compute is NumPy on CPU. The unattended unit and linger are separate consents:
   `python -m mycelium_a13_linux.service_install`.

## 7. If a LAN peer cannot be reached

Run `python -m mycelium_demo doctor --lan` first; it covers the configuration
causes. The items below are what it cannot see.

- Wi-Fi "client isolation" (guest networks, hotels) blocks device-to-device
  traffic. There is no relay fallback in `lan` mode by design.
- Multiple interfaces (VPN, Docker, tethering): auto-selection takes the primary
  route. Pass `--bind` / `--advertise` explicitly.
- Firewalls: allow the seed port (TCP 8765 by default), and later the sidecar's UDP.
- Clock skew: leases are time-based and there is no NTP client in the product;
  keep the machines' clocks within a few seconds.

A private swarm across different networks (join link, third parties, time limits): see [FRIEND-SWARM.md](FRIEND-SWARM.md).
