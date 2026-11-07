# Mycelium (Rust port, v0.2 — in development)

Local working repository for the v0.2 Rust port. Not published; no remote.
Licence: AGPL-3.0-only (see LICENSE). Every source file carries an SPDX header.

Authority: `/Users/mycelium-dev/Documents/mycelium-core-program/status/V02-PLAN.md`
(scope, lanes, foundation-first order) and the adopted pipeline (`V02-PIPELINE.md`,
`TESTING-STRATEGY.md`). The behavioural oracle is the accepted Python v0.1.4 artifact,
driven only from outside as a process/HTTP endpoint. No Python in this repository.

Layout: `crates/` product libraries, `test-support/` shared test infrastructure
(obligations registry, budget model, simulation), `xtask/` workspace tooling (lints, preflight).
