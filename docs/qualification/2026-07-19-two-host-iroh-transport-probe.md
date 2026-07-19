# Two-host authenticated Iroh transport probe

Date: 2026-07-19

Status: **PARTIAL physical evidence; transport boundary validated, route not qualified.**

## Question and verdict

Given two separate Apple-silicon Macs on the same phone-provided network, can the
integrated production `IrohTransport` and native Iroh sidecar exchange canonical
Router frames bidirectionally, authenticate each local Python/sidecar session,
and return only after remote Python dispatch acknowledgement?

**VALIDATED for that bounded question.** Three fresh two-host runs exchanged and
remotely dispatched every synthetic frame. This is not full Router execution,
model inference, stage/load qualification, a direct-LAN-path proof, or release
qualification. `route_ready=false` and `release_ready=false` throughout.

## Immutable source and hosts

- source commit: `dccb44c24104431dd08bba538a5f922cff285a86`
- integration branch: `integration/mycelium-active-session456-20260718`
- coordinator: `m4pro`, arm64, macOS 26.5.1
- physical peer: `Evis-MacBook-Pro`, arm64, macOS 15.6.1
- peer interpreter: `/opt/homebrew/opt/python@3.14/bin/python3.14`, Python 3.14.2
- sidecar binary SHA-256 on both hosts:
  `a02362ba1e743fb076c23dff4b83a1eecfd20ecc6abc6c23c9717980f7c1653b`

The coordinator reached the peer over its local SSH hostname. The sidecars'
ready records advertised only `Ip` address entries during these runs: four on
`m4pro`, two on `Evis-MacBook-Pro`. The sidecar evidence does not expose the
selected Iroh path, so this does **not** prove that packets used a direct LAN
path rather than another Iroh path.

## Probe boundary

The throwaway spike did the following on each physical host:

1. generated a fresh 32-byte bootstrap secret in memory;
2. passed it to the native sidecar through an inherited descriptor, never argv,
   environment, stdout, or evidence;
3. used a run-scoped mode-0700 directory and a sanitized child environment;
4. started the production `mycelium_router.transports.iroh.IrohTransport`;
5. bound a thread-safe capture adapter implementing only
   `receive_path_cancellation` and `receive_token_event`;
6. configured a pinned peer EndpointID/address/generation binding;
7. sent canonical frames through production encode/decode, sidecar
   authentication, Iroh transport, remote adapter dispatch, and authenticated
   remote acknowledgement.

The capture adapter intentionally was not a production `Router`. Therefore the
probe verifies the physical transport/dispatch boundary, not Relay cancellation
cleanup or end-to-end inference semantics.

The peer received 57 MiB of explicitly staged tracked Python sources, the
already-built sidecar binary, and the probe node. No package install, source
fetch, model download, build, push, or credential transfer occurred.

## Observed runs

Each passing run created fresh sidecars and fresh, distinct EndpointIDs.
Endpoint IDs are represented only by hashes here; raw private evidence is not
tracked in Git.

| Run | `m4pro` -> peer | peer -> `m4pro` | Negative rejections | Evidence SHA-256 |
|---:|---:|---:|---:|---|
| 1 | 16 `PathCancellation` | 16 `TokenEvent` | 4 | `4f631cba92dd1dc7de036d818a61142f8435eb9a69dfb6f5472f37b5cb721c4e` |
| 2 | 16 `PathCancellation` | 16 `TokenEvent` | 4 | `279b3157a9af9ff79035deab7c08f27829340ce608238f8c5540fa8122ff3ccc` |
| 3 | 16 `PathCancellation` | 16 `TokenEvent` | 4 | `e7e5513b0ed943facbca7dc53387ce1de464e918ee92b610d03870e095a84411` |

Aggregate observed result:

- 48/48 `m4pro` -> peer frames returned
  `remote_router_dispatch_ack`;
- 48/48 peer -> `m4pro` frames returned
  `remote_router_dispatch_ack`;
- 96/96 frames appeared in the expected remote capture adapter;
- each host reported 16 sent, 16 received, 16 dispatched, and zero duplicate
  frames in each run;
- all 12 negative probes failed closed with the expected stable code:
  `destination_binding_mismatch` or `malformed_router_frame`;
- all nine recorded route-readiness values were `false`.

The canonical frame vectors were stable across all three runs:

- `PathCancellation` vector:
  `sha256:7f9bc503e37cbc1987667d6e7d96399aad8bfcb3cfe305ca67b5f572aac288af`;
- `TokenEvent` vector:
  `sha256:4c127a7fe7285ae1788ecf3173aeb0f43ab1937df8b71f81e2f316e71de6cad6`.

## Spike correction and evidence preservation

The first exploratory attempt sent one valid cancellation, then stopped locally
before the reverse send because its synthetic `TokenEvent` used
`sampling_counter=0`. Canonical wire validation correctly rejected it as
`invalid_token_event`. The spike fixture changed to `index + 1`; no production
code changed. The three runs above are the subsequent independent passes.

Private raw evidence and the exact throwaway scripts are preserved mode 0600 at:

`/Users/evinova-self/mycelium-physical-qualification-evidence/two-host-iroh-20260719/`

Archive checksums:

- node spike:
  `2fd4237a65684b0d6f951d69b52e36f133025f42f7b71de06201edfef147c318`;
- coordinator spike:
  `0fe8d99514483c86fb1f5f5b0e03f84665e7b62bc16f9f1af06cad7a5f1162b9`;
- `SHA256SUMS` verified every archived file after copyback;
- automated marker scan found no `bootstrap_secret`, private-key, bearer-token,
  or password marker in the three JSON evidence files.

Run-scoped local and peer staging trees were removed after copyback. Both hosts
then reported zero sidecar and zero probe-node processes. The private evidence
archive remains outside the repository.

## Exact claim boundary

Observed:

- two distinct physical Macs;
- authenticated local sidecar sessions on both Macs;
- native Iroh endpoints on both Macs;
- production `IrohTransport` on both Macs;
- canonical Router frames in both directions;
- production destination binding and malformed-frame rejection;
- remote Python dispatch before sender acknowledgement;
- clean endpoint restart across three independent lifecycles.

Not observed:

- production `Router`, Entry, Relay, scheduler, or runtime execution across the
  physical link;
- model weights, tensor transfer, token parity, or inference;
- direct-LAN selected-path telemetry;
- packet loss, disconnect, cancellation races, replay after process loss, or
  recovery;
- physical stage/load proof, qualifier signature, or Observatory ingestion;
- latency, throughput, memory, energy, or performance qualification;
- Pixel 8 participation.

This evidence narrows the native-Iroh uncertainty but cannot promote a route.
`route_ready=false`; `release_ready=false`; local lab evidence only.
