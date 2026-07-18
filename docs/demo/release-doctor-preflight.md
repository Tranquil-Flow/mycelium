# Release doctor preflight

This tranche adds only a read-only local prerequisite check. Run it from the repository root with Python 3.14 and an explicit state directory outside the source tree:

```sh
python3.14 -m mycelium_demo doctor \
  --state-dir /tmp/mycelium-state \
  --port 9021 \
  --port 9022
```

The command checks required local tool executables, critical checked-in files, the external state-directory boundary, and availability of any explicitly supplied loopback TCP ports. A port probe binds and immediately closes a loopback socket; it does not reserve the port.

The command does not start processes, create the state directory, install dependencies, provision model artifacts, contact peers, run inference, mutate Observatory, or consume qualification evidence. No package installation or network access is performed.

Exit status 0 means only `local_preflight_ok=true`. It never means an inference route or release is accepted. This initial report always carries:

- `route_ready=false`
- `release_ready=false`
- `qualification_evaluated=false`

It does not perform physical qualification. RouteQualificationV1 consumption, process orchestration, immutable evidence writing, bounded cleanup, physical two-host execution, request streaming, read-only live UI proof, and recovery remain later integration gates.

The report intentionally omits timestamps, host identity, environment contents, executable paths, and secret-bearing arguments. JSON serialization is canonical for deterministic local comparison, but this preflight report is not accepted route evidence and must not be placed in a physical qualification evidence bundle as proof of readiness.
