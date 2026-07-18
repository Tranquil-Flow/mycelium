# Claim-boundary audit

`scripts/claim_boundary_audit.py` is a deterministic, read-only release gate for two narrow Mycelium invariants:

1. literal `route_ready=true` production claims may appear only in `mycelium_qualification/qualifier.py`, while literal `release_ready=true` claims remain forbidden;
2. the Observatory backend and browser source expose no statically visible `POST`, `PUT`, `PATCH`, or `DELETE` write surface.

Run from the repository root:

```bash
python3.14 scripts/claim_boundary_audit.py --repo-root . --json
```

Exit `0` means the tracked source satisfied these static boundaries. Exit `1` means at least one finding or fail-closed inventory/source error occurred. JSON output uses protocol `mycelium.claim_boundary_audit.v1`, sorted keys, deterministic finding order, and no timestamps or absolute repository paths.

## Read-only behavior

The audit inventories Git-tracked files with `GIT_OPTIONAL_LOCKS=0`. It does not checkout, stage, commit, reset, clean, fetch, push, install packages, bind ports, contact peers, or write evidence. Missing, symlinked, unmerged, malformed, unreadable, oversized, concurrently changed, or unparsable tracked production source fails closed. Individual scanned source files are bounded to 4 MiB.

Test files are excluded from production-claim scanning. The UI check targets non-test JavaScript and TypeScript beneath `ui/web/src/`. The backend check targets Python beneath `mycelium_gateway/`. The readiness check examines literal-true Python mappings, assignments, subscript assignments, and keyword arguments outside tests.

## Claim boundary

Every result fixes these claims:

- `route_ready=false`
- `release_ready=false`
- `semantic_qualification_evaluated=false`
- `physical_qualification_evaluated=false`
- `authenticated_transport_evaluated=false`
- `runtime_semantics_evaluated=false`
- `dynamic_dispatch_evaluated=false`

Passing means only that tracked production source contains no forbidden literal claim or statically visible Observatory write surface covered by this version. It does not run inference, inspect dynamic framework registrations, prove qualifier semantics, evaluate transport, or perform physical qualification. It does not authorize the request gateway, release, deployment, or any `route_ready=true` statement. Observatory remains read-only.
