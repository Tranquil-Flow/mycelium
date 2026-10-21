# M11 successor baseline fixtures

## Status and claim boundary

This specification freezes the public compatibility surface that M12 and later
milestones inherit from the sealed M11 implementation. The fixtures are deterministic,
synthetic compatibility documents. They are not physical qualification evidence, do
not assert that a route is currently live, and must never be promoted into the live UI
source lane.

The canonical registry is `scripts/contract_registry.py`. The generated fixture bytes
live under `contracts/compatibility-fixtures/`, and
`contracts/contract-manifest.v1.json` pins every fixture and owning implementation
source. Unknown fixtures, protocol aliases, duplicate protocol owners, path escapes,
symlinks, and byte drift fail the contract audit closed.

## Frozen families

The baseline covers:

1. signed membership envelopes;
2. planner input/output and the atomic control-plane tranche;
3. assignments, artifact reports, provisioning audit, and load proofs;
4. execution graph and graph-bound path manifest;
5. live route runtime/KV projection;
6. the Router binary transport golden index;
7. deterministic replan/recovery reports;
8. route qualification, request submission, and request events;
9. product bootstrap; and
10. privacy-reduced Observatory snapshot and event envelopes.

## Executable acceptance

Generation must be byte-deterministic. Python consumers verify signatures, decode and
validate graph/path bindings, decode every pinned Router frame, rerun the recovery
simulation, and decode the semantic snapshot/event. TypeScript consumers validate the
product bootstrap schema and decoder and decode the runtime/KV projection. The full
contract audit then hash-pins the fixture bytes and all declared owners.

No fixture may contain a private key, seed database, bearer credential, prompt, model
output, token IDs, activation values, hidden states, KV contents, filesystem paths to
private operator material, or private network endpoints. Runtime/KV fixtures expose
only bounded counts and release reasons; transport fixtures expose hashes and public
message-type metadata, with binary frames kept in their existing golden directory.
