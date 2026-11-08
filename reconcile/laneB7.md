# Lane B7 — phase-2 precursor: member shard byte custody

Base: `1bb931978f08799c97e09bb6da012145e2058a9f`; branch `lane/b7-two-node-serving`.
Disk blocker cleared (39 GiB at resume). This is NOT the requested serving journey.

## Additive boundary implemented

- `api::compiler_inputs::member_shard::MemberShard` receives one independently
  pinned transfer row from a node recipe without needing the source checkpoint.
- Reuses B3's exact ledger rows, consent cap, headroom and reservation-before-write
  semantics. `reserve_output` is extracted without changing old compiler behavior.
- Exclusive per-run custody, bounded append, byte-identical duplicate retry,
  no sparse/overlapping writes, fsync before acknowledgment, retained debt on Drop.
- Transfer hash/length and contiguous recipe range hashes/inline bytes checked;
  caller checkpoint before each bounded hash read. Not a native tensor proof.
- Explicit inert-byte discard validates retained file and directory identities,
  unlinks, fsyncs, writes a versioned refund marker, then removes the ledger charge.
  Restart resumes partial custody; refund retries survive pre-rename interruption.
- Existing ledger temporary recovery additively accepts an omitted row ONLY with
  its matching durable refund marker, unchanged run identity and absent output
  directory. It discards the temporary; never adopts it or refunds by inference.
- `fetch_chunk` consumes the existing signed `ArtifactChunk` QUIC carrier and
  rechecks a host-supplied admission checkpoint before/after network I/O.

## Compatibility and ownership

Class B additions: `mycelium.member_shard_custody.v1` and
`mycelium.member_shard_refund.v1`, private local records only. Existing B3 ledger
rows and all wire/signature bytes unchanged. No dependencies, lockfile, reserved
S1/chat/status/network-cancellation edits. Ruling 10 authorizes the additive API.
No API-to-backend edge. No changed ready level; `route_ready=false` remains.

## Executed proof and remaining gap

The focused carrier test uses the product `LoopbackHost`, `ArtifactService`,
`DurableObjects` and `ChunkSession` in two exact-owned OS processes, separate
roots and endpoint/signing identities, relay/discovery disabled. It transfers
actual bytes from the independent source-slice corpus, verifies signed receipts,
checks retries/revocation-after-read, refunds and reaps/rebinds sockets. Its grant
issuer and admission checkpoint are CONTROLLED fixtures, not real app pairing.
No models, resident proofs, challenge tokens or plans were synthesized.

This is a verified sub-boundary WITHIN phase 2, not phase 2 completion:
1. Wire real pairing/current member generation/consent to the checkpoint and pins.
   Artifact grant v1 itself does not bind membership generation; do not relabel it.
2. Owner source publication still uses DurableObjects' whole-object `verified_size`
   read; qualify/replace with bounded scanning before large model transport claims.
3. Compose a node-owned receiver with headless `slices_ports` / `work_ports` and
   source-derived compile output; no public serving launcher exists here.
4. Bind member native proof to identity; cross-node admission, M16 executor,
   real chat, Stop, worker death/stall and leave remain unimplemented.
5. Discard is for inert bytes only. Future serving ownership must prove worker reap
   BEFORE calling it; path possession is not execution or cleanup authority.
6. Interrupted initial creation before the file-identity record commits fails closed
   with retained debt; automatic reconciliation of that narrow window is not supplied.
7. Physical no-public-relay LAN configuration and TWO-MAC-TEST launch/action commands
   remain blocked on the actual serving composition. Never use N0 as a shortcut.

No demonstrated need to change Evis-reserved behavior; these are implementation
follow-ups, not a request for wider permission. Final gates are reported separately.
