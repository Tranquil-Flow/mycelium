# Multi-device swarm onboarding runbook

This runbook adds multiple **trusted, invited native devices** to durable membership.
It deliberately does not put them into the active inference route. Read
`docs/contracts/external-tester-boundary.md` before distributing any invitation.

## 1. Start one durable seed

Choose owner-only state and outbox directories on the operator host. The example
address must be replaced by that host's private Tailscale IPv4 address. Initialization
is a separate one-time operation and is the only command allowed to create the seed
identity and database:

```sh
mkdir -m 700 /absolute/private/mycelium-seed
/absolute/path/to/python3 -m mycelium_seed \
  --data-dir /absolute/private/mycelium-seed \
  --swarm-id mycelium-swarm \
  --seed-node-id seed-node \
  --init-only
```

Then start the load-only service:

```sh
/absolute/path/to/python3 -m mycelium_seed \
  --bind 100.x.y.z \
  --port 8765 \
  --advertised-url http://100.x.y.z:8765 \
  --data-dir /absolute/private/mycelium-seed \
  --swarm-id mycelium-swarm \
  --seed-node-id seed-node
```

Keep this process running. Reuse the same private data directory on restart; changing
it creates a different seed identity. Do not bind the current HTTP endpoint to a
public interface.

## 2. Mint one credential per device

In a second terminal on the seed host:

```sh
/opt/homebrew/bin/python3.14 scripts/mint_swarm_invites.py \
  --seed-data-dir /absolute/private/mycelium-seed \
  --seed-url http://100.x.y.z:8765 \
  --swarm-id mycelium-swarm \
  --output-root /absolute/private/mycelium-invite-outbox \
  --count 8 \
  --ttl-seconds 900
```

The tool first verifies that the live endpoint presents the signer stored in the
durable seed directory. It then creates one new mode-`0700` batch directory containing
mode-`0600` `native-node-NNN.invite.json` files. `manifest.json` contains only file
digests and public binding metadata; it contains no invite token. Existing batch
directories are never overwritten. The JSON printed to stdout is sanitized and
always says `route_ready=false`.

Deliver exactly one invite file to each known user/device over a separate
authenticated private channel. Delete or securely archive unused expired files using
an exact resolved path. Never paste a bundle into chat, logs, the browser UI, or a
command-line argument.

## 3. Join each native device

Install Mycelium and its reviewed Iroh sidecar on the target native device. Create an
owner-only device state directory, place its one invite file at an owner-only path,
and run:

```sh
/absolute/path/to/python3 -m mycelium_node \
  --data-dir /absolute/private/mycelium-node \
  --join-bundle-file /absolute/private/native-node-001.invite.json \
  --node-id unique-device-name \
  --lifecycle-state CONFIGURED \
  --advertise https://device-private-control.example/control \
  --sidecar-path /absolute/path/to/mycelium-iroh-sidecar
```

For an Android/Termux conformance device, add the generic mobile class and the Iroh
EndpointID produced by that device's reviewed sidecar:

```sh
  --peer-class android_termux_iroh \
  --membership-endpoint-id exact-iroh-endpoint-id
```

This class is not tied to Pixel hardware; Pixel 8 is only the first physical test
device. The runtime implementation is currently named `pixel-stdlib` for compatibility
and should be treated as naming debt, not as a device-specific protocol.

`--advertise` must be the device's canonical `http` or `https` control endpoint. Use a
unique stable node ID and state directory per device. The node creates and reuses its
own mode-`0600` signing key, joins with the single-use token, and maintains signed
heartbeats. A bundle supplied with `--join-bundle-stdin` is also supported when the
transfer mechanism can pipe it without persisting or logging it.

Do not share a device state directory, clone a device private key, or start two users
under one node ID. If a target cannot run the reviewed native sidecar/runtime, enroll
it through Device Lab as a visibly activation-ineligible probe instead.

## 4. Qualification gate before inference

After membership is visible and heartbeats are current, keep the existing qualified
deployment selected. For every proposed new native stage:

1. collect signed capability, storage, memory, backend, and directed-link evidence;
2. choose contiguous layer ranges and device order from the approved placement path;
3. build and deliver only assignment-bound stage artifacts;
4. prove artifact digest, runtime load, endpoint identity, and stage parity;
5. rebuild the proposed full topology and run startup plus physical qualification;
6. select it only after the qualification authority reports it ready.

Enrollment alone must never change Router state, deployment selection, or the model
shown in Inference. Browser/legacy `pixel_http` workers remain evidence-only. An
`android_termux_iroh` member is merely activation-capable until separately qualified;
it must not enter the selected production route on membership alone.

## 5. Inspect, revoke, back up, and restore membership

These commands take the seed's exclusive owner-only process lease. Stop the seed
service cleanly before running one, then restart it against the same state root after
the command succeeds.

Inspect the privacy-reduced durable inventory:

```sh
/absolute/path/to/python3 scripts/manage_seed_membership.py inventory \
  --seed-data-dir /absolute/private/mycelium-seed
```

Revoke exactly one observed generation. A stale expected generation fails closed:

```sh
/absolute/path/to/python3 scripts/manage_seed_membership.py revoke \
  --seed-data-dir /absolute/private/mycelium-seed \
  --node-id exact-node-id \
  --expected-generation 2 \
  --reason operator_revoked
```

Create one new owner-only backup generation and retain its printed leaf directory:

```sh
/absolute/path/to/python3 scripts/manage_seed_membership.py backup \
  --seed-data-dir /absolute/private/mycelium-seed \
  --output-root /absolute/private/mycelium-seed-backups
```

Restore only into an absent target root. Restore verifies file types, ownership,
modes, hard-link count, key/database identity binding, manifest digest, SQLite
integrity, and member count before making the result available:

```sh
/absolute/path/to/python3 scripts/manage_seed_membership.py restore \
  --backup-root /absolute/private/mycelium-seed-backups/backup-exact-id \
  --target-root /absolute/private/restored-mycelium-seed
```

Never combine a key from one backup with a database from another. If both the live
coordinator identity and every complete backup are lost, initialize a new swarm with
a new swarm ID and re-enroll devices; do not present it as continuity. Planned signer
rotation commands now exist, but must not be used on a qualified deployment until
every enrolled native agent is running the overlap-aware build and the maintenance
gate has recorded that it learned the dual-signed transition. Code availability is
not physical rotation evidence.

## 6. Current operating constraints

- All external devices are trusted invitees on the operator's private Tailscale
  network. Internet-native direct/relay onboarding remains M20.
- The live product page presents membership/onboarding as operator-only because raw
  credentials do not belong in a loopback browser projection.
- macOS `caffeinate` prevents ordinary idle sleep but does not make a closed laptop lid
  a supported server configuration. Use powered clamshell mode or make a separate,
  reversible administrator power-management decision.
- Up to 64 invites may be minted per batch; the practical stage count is determined by
  measured capacity, model shape, network cost, artifact load, and qualification.
