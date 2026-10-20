# Running Mycelium locally (local-first runbook)

Goal: your own seed, nodes and UI on your own network, with no `mycelium.now`,
no hosted directory and no third-party relay. This page says what works **today**,
what the new `--network-mode lan` adds in this lane, and what is **not possible
yet**. Labels used below:

- **TESTED (unit)** - covered by tests in `tests/network_mode/`; no socket was opened.
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
- **Route bring-up over LAN.** `mycelium_live/route.py` forwards only
  `--sidecar-force-relay`; the operator plans use `ssh_target`.
- **Discovery.** There is no mDNS/Bonjour. Peers learn each other's addresses from
  the membership exchange; you pass or auto-derive the seed address.
- **Offline model bytes.** Hugging Face is the last fallback after owner LAN and
  relay sources. There is no `--offline` flag yet. Pre-seed the model and set
  `HF_HUB_OFFLINE=1` (library behaviour, UNVERIFIED in this tree).
- **Transport security of the seed.** The seed endpoint is plain `http`. Use it
  only on a network you control; invites are single-use and signed but the
  channel is not encrypted. TLS for LAN origins is not implemented.

## 4. Privacy

SOURCE-READ across product code: no telemetry, analytics, crash reporting,
update check, NTP, external CDN or font. The things that can leave your network
are: n0 relays/DNS (`internet` sidecar), Cloudflare (A13 public relay and its
DoH fallback), Hugging Face (model download fallback), and package indexes at
install time. In `lan`/`local` mode none of these is configured by the commands
below, but until the sidecar LAN mode exists that is only true for membership.

## 5. Runbook: a private seed and a membership-only node on one LAN

All commands UNVERIFIED on hardware. Python 3.14 is what the repo and CI use.
Use whichever `python` your OS provides, via `python -m`, rather than a
hard-coded path. State directories must be owner-only (`chmod 700` on macOS and
Linux; the Windows installer applies ACLs).

On the seed machine:

```sh
export MYCELIUM_NETWORK_MODE=lan                      # or pass --network-mode lan
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

- Wi-Fi "client isolation" (guest networks, hotels) blocks device-to-device
  traffic. There is no relay fallback in `lan` mode by design.
- Multiple interfaces (VPN, Docker, tethering): auto-selection takes the primary
  route. Pass `--bind` / `--advertise` explicitly.
- Firewalls: allow the seed port (TCP 8765 by default), and later the sidecar's UDP.
- Clock skew: leases are time-based and there is no NTP client in the product;
  keep the machines' clocks within a few seconds.
