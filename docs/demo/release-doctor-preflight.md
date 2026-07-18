# Release doctor preflight

This tranche has one bounded claim surface:

- Local environment preflight only.
- No physical-host evidence.
- No qualification consumption.
- No release-readiness claim.

Run it from the repository root with Python 3.14 and an explicit state directory outside the source tree whose immediate parent already exists and is writable:

```sh
python3.14 -m mycelium_demo doctor \
  --state-dir /tmp/mycelium-state \
  --port 9021 \
  --port 9022
```

The command validates local executable probes, critical checked-in regular files, repository and state-directory path boundaries, and any explicitly supplied loopback TCP ports. Repository and required-file checks reject symlinks and use read-only descriptor-relative traversal. Input collections are validated before probes and normalized into deterministic report order.

A port result is a point-in-time advisory only. The probe binds and immediately closes one loopback socket; it does not reserve the port and is not route evidence.

The command does not start processes, create the state directory, mutate files, install dependencies, provision model artifacts, contact peers, open transport connections, run inference, mutate Observatory, or consume qualification evidence. No package installation or network access is performed. It does not perform physical qualification.

Exit status 0 means only `local_preflight_ok=true`; status 1 means the local preflight failed closed. Neither status accepts a route or release. Every report unconditionally carries:

- `route_ready=false`
- `release_ready=false`
- `qualification_evaluated=false`

Remaining release blockers are exact and external to this tranche:

- Request gateway waits for the qualification authority commit and schema freeze.
- Recovery integration waits for stable KV and iroh path plus physical base-route proof.
- The Observatory request and qualification event adapter waits for both producer contracts.
- Physical two-Mac qualification requires explicit authorization plus a staging and cleanup plan.

The report intentionally omits timestamps, host identity, environment contents, input values, executable paths, absolute filesystem paths, internal exception text, and secret-bearing arguments. JSON serialization and order-insensitive input normalization make equivalent reports canonical for deterministic local comparison. The report is not accepted route evidence and must not enter a physical qualification evidence bundle as proof of readiness.
