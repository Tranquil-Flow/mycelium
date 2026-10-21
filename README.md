# Mycelium

Mycelium splits one language model across several computers, so machines that
could not run the model alone can serve it together. It assigns model layers to
devices by available memory, sends each device only the weights it needs, passes
activations from device to device, and streams the answer back token by token.

**Status: first public preview (V1).** It works on the project's own Macs and has
not been tested on a wide range of machines. Read the
[release notes](docs/RELEASE-NOTES-V1.md) for the exact list of what works and
what does not.

## What works today

- **A two-Mac swarm.** One Mac creates a swarm, a second Mac joins with a
  short-lived, single-use code, and the model's layers are split between them.
  Chat answers stream in the app.
- **A public directory.** A swarm can be listed on <https://mycelium.now>, and
  others can chat with it through the swarm's public entry.
- **Your own relay origin.** The owner can serve the swarm's public entry from a
  hostname they control instead of a temporary tunnel address.
- **Verified downloads.** Each Mac fetches only its slice of the model; downloads
  are checked against pinned hashes and can resume.

Limits worth knowing before you start: macOS on Apple silicon only; the app is
ad-hoc signed and not notarized; the public demo model is small (Qwen2.5-0.5B);
only two-Mac swarms have been run end to end; and the Macs that serve a prompt
can see it (see [`docs/user/PRIVACY.md`](docs/user/PRIVACY.md)).

## Install (macOS app)

1. Download the macOS app zip and its `.sha256` file from the
   [latest release](../../releases/latest).
2. Follow [`docs/user/QUICKSTART-MACOS.md`](docs/user/QUICKSTART-MACOS.md): check
   the checksum and signature, run the installer, then start or join a swarm with
   two Macs.
3. If something goes wrong, see
   [`docs/user/TROUBLESHOOTING.md`](docs/user/TROUBLESHOOTING.md).

The other pages in [`docs/user/`](docs/user/README.md) explain what Mycelium does
and how far each feature has been proven.

## Developer setup

You need Python 3.14 (the Homebrew build is what the project uses). In a fresh
virtual environment:

```bash
/opt/homebrew/bin/python3.14 -m venv --system-site-packages .venv
.venv/bin/pip install cryptography numpy -r requirements-dev.txt
```

`requirements-dev.txt` adds `pytest-xdist` and `playwright`. `pytest` and
`pytest-asyncio` come from the interpreter's own site-packages through
`--system-site-packages`; on a bare interpreter also run
`pip install pytest pytest-asyncio`. Sharded MLX inference needs `mlx` on Apple
silicon (`pip install mlx`).

Run tests with the repository root on `PYTHONPATH`, one directory or file at a
time:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q -p no:cacheprovider tests/invite
```

The test levels (unit, integration, emulation, physical) and their markers are
described in [`pytest.ini`](pytest.ini). Tests under `physical_*` directories need
real hardware.

Web interface:

```bash
cd ui/web
npm ci
npm run check
```

Offline two-process MLX runtime-load qualification:

```bash
.venv/bin/python two_process_runtime_qualification.py --json
```

It generates all model artifacts locally, provisions through an injected
local-only fetcher, runs the stages in `multiprocessing` `spawn` children, and
emits JSON-safe load proofs. A Python audit guard rejects socket and DNS events
from just before each assignment load until the child exits; this is not an
OS-level network sandbox. It does not claim distributed inference.

Interactive browser swarm on one machine (a local qualification surface):

```bash
.venv/bin/python scripts/interactive_swarm_server.py
```

Open the printed `operator_url`, create a join link, and open it in the
contributing browser. Use beyond loopback needs HTTPS and an explicit
`--public-origin`; see [`docs/interactive-browser-swarm.md`](docs/interactive-browser-swarm.md).

## How it fits together

```text
Gossip -> Planner -> assignments -> provisioning -> runtime load proofs
       -> Layer Builder -> Router -> stage execution -> qualification
```

Control-plane records never carry model tensors. Runtime readiness and route
readiness are separate states.

| Area | Where |
| --- | --- |
| Layer planning and re-planning | `mycelium_layer_planner/`, `layer_planner.py` |
| Request and inter-layer routing | `mycelium_router/` |
| Peer discovery and evidence gossip | `mycelium_gossip/` |
| Signed seed, invitations, memberships | `mycelium_seed/`, `mycelium_invite/`, `mycelium_membership/` |
| Public swarm directory | `mycelium_swarm_directory/` |
| macOS app | `mycelium_a13_macos/` |
| Web interface | `ui/web/` |

Component designs: [`ALLOCATOR.md`](ALLOCATOR.md),
[`GOSSIP_PROTOCOL.md`](GOSSIP_PROTOCOL.md),
[`BROADCAST_PROTOCOL.md`](BROADCAST_PROTOCOL.md),
[`REQUEST_AND_INTER_LAYER_ROUTER_DESIGN.md`](REQUEST_AND_INTER_LAYER_ROUTER_DESIGN.md),
[`FAULT_TOLERANT_LAYER_REPLANNER.md`](FAULT_TOLERANT_LAYER_REPLANNER.md), and
[`LAYER_PLANNER_PRODUCT_V1.md`](LAYER_PLANNER_PRODUCT_V1.md).

## Reporting problems

Open a GitHub issue and include the app's short error code. Never paste secrets,
tokens or private keys. Evidence committed to this repository must be redacted
and addressed by a manifest; the Network Observatory stays read-only.

## Licence

Mycelium is licensed under the GNU Affero General Public License, version 3 only
(`AGPL-3.0-only`). See [`LICENSE`](LICENSE).
