# Lane B7 — blocked before implementation

Base: `1bb931978f08799c97e09bb6da012145e2058a9f`.
Branch: `lane/b7-two-node-serving`.

## Current result

No product changes, builds, tests, native runs or node launches. `df -g /` reported
24 GiB available initially, after each of two 120-second waits, and at the final
source-inspection check (2026-11-08T13:31:12Z). This is below the task's 25 GiB
hard floor and 27 GiB build admission threshold. No sibling process/cache was
touched. Only this reconciliation document is committed.

## Source-bound continuation seams

- `crates/api/examples/headless.rs:1773-1833`: `HeadlessChainPorts` still has no
  `slices_ports` or `work_ports`. Add a host-owned serving composition using the
  existing public chain APIs; do not replace S1 or packaged chat behavior.
- `crates/api/src/native_compiler.rs:387-478`: both stages are local child workers;
  challenge drives their Unix sockets and closes both before returning artifacts.
  Reusing this result is not live member execution. Remote preparation needs an
  additive stage-session interface retaining actual member worker custody.
- `crates/stage-contract/src/native_proof.rs:12`: resident proof is explicitly a
  private socket observation, not a signed remote attestation. Preserve its bytes;
  add a versioned member-signed envelope binding the actual returned proof to
  endpoint identity, current pairing generation/incarnation, plan and challenge.
- `crates/runtime/src/iroh_host.rs:94-200`: existing LocalOnly binding uses Minimal,
  no relay/discovery, explicit loopback, and supports caller-owned endpoint keys.
  Use this real carrier; bind its peer identity independently to current membership.
- `crates/api/src/compiler_inputs.rs:307,582-605`: B3 reservation is attached to
  `InputArtifact`; completion retains charges. No public member receive/release
  adapter was found in that module. Extend the ledger additively, preserving
  inode/owner checks and retained cleanup debt; never refund merely on Drop.
- `crates/api/src/m16_requests.rs:40-65`: implement the existing Host/Executor seams.
  Executor calls must remain bounded/nonblocking; terminal follows proven cleanup,
  not Stop delivery. Stream actual greedy outputs, not reference fixture outputs.

## Required boundaries

New wire/signed messages require explicit versions; existing message bytes and
reserved behavior remain unchanged. Workspace-internal edges are allowed under
ruling 10 and require a reconcile note; external packages require approval.
The B6 sandbox is trusted-local-worker hygiene, not hostile-worker confinement.
Do not promote it when introducing a remote member. Physical `route_ready` must
remain false on one Mac, even after a future loopback two-node qualification.

No Evis-owned behavior change has been demonstrated necessary by this inspection.
The immediate unblock is disk headroom, not permission to change reserved code.
RU-053 pins, all gates and all timeout budgets remain untouched.

## Resume

Wait for at least 27 GiB available; inspect branch/status and current m4/main,
then implement/test the requested phases in order. The separate
`~/Projects/mycelium-m4-program/TWO-MAC-TEST.md` is a blocked procedure outline,
not an executable or qualified physical-serving harness. Replace its missing
launch/action commands with the actual implemented public surface before use.
