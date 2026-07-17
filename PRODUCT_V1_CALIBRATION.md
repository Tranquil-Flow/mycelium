# Product V1 calibration gate

Product V1 accepts immutable calibration artifacts through
`mycelium_layer_planner.calibration.ingest_calibration`.

A measured artifact must contain:

- immutable model ID and revision;
- backend and device identity;
- half-open layer range and precision;
- raw prefill and decode timing samples;
- payload and batch/context calibration points;
- timestamp and environment;
- a reproducible measurement command.

Heuristic node coefficients remain permitted for planning simulations, but they
are labeled heuristic and must not be represented as measured performance.

Current repository status: calibration schema and ingestion PASS. Live
model/backend calibration SKIPPED because this build session has no selected
model weights or serving backend. No measured performance claim is made.
