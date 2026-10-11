# A15 executed-artifact intake (operator note)

**Status:** additive slice under the A15 primary gate. A15 itself remains `design_only`.

`mycelium_a15_run_intake.py` + `scripts/a15_run_intake.py` implement a bounded, fail-closed
**intake** over real retained artifacts. They read bytes, verify what the artifacts claim about
themselves, and emit an explicitly **not-ready** report. They are not the A15 production verifier and
they assemble no release decision.

## Run it

```sh
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV /opt/homebrew/bin/python3 -B \
  scripts/a15_run_intake.py --now 2026-09-21T12:00:00Z --report /tmp/a15-intake.json
```

Without `--declaration` the CLI intakes the retained pilot run
(`runs/wp8-02-observational-p84r1`) plus the retained completion records and the frozen checklist,
with the artifact digests pinned in the script. With `--declaration` it intakes any closed-shape
declaration; see `validate_declaration` for the schema.

Exit codes: `0` report produced (the report will normally contain findings — findings are the
deliverable), `2` the intake refused to run (a declaration/shape error, a `--declaration` path that
cannot be opened, or a `--now` that is missing/non-UTC/unparseable; no report and no claim is emitted
on this path), `3` the report could not be written. Refusal is always fail-closed and never a raw
traceback.

## What it checks

Declared digest (recomputed from the file), declared protocol against an allowlist grounded in real
observed protocols, source commit present in the audited worktree and equal to every head measured in
the run artifacts, provenance class (a `fixture`/`replay` input never satisfies a mandatory executed
artifact), evidence expiry (**both** the declared `fresh_until`/`observed_at` window on each declared
artifact and the sealed-evidence windows in the completion records; a closed window against the
caller's `now` is a finding — `mandatory_evidence_expired` — and is never ignored), path containment
(absolute paths and `..` components are refused, and containment is re-checked on the *resolved* real
path so a symlinked intermediate directory cannot resolve outside the declared root), sealed-evidence
and checklist-binding digests, and the 12 `PACKAGE.json` file digests. Every missing, unreadable,
undeclared or mismatching input is a finding; nothing unsettled is treated as a pass.

## Pinned bytes: working path vs preserved original

`runs/wp8-02-observational-p84r1` is a **mutable working directory**: the pilot/A13 lane re-runs
overwrite `readiness-gate.json` and `PACKAGE.json` in place. The exact bytes the A15 handover pinned
survive inside that directory as the preserved originals
(`readiness-gate-preserved-fb0314c.json`, `PACKAGE-preserved-0.1.0-preview.84.json`). Exact
retained-artifact checks — and the declarations the intake is exercised over — bind **those preserved
paths**, never the mutable working files. The pinned digests themselves are unchanged; the working
files are read separately and any divergence from the pin is reported as
`artifact_digest_mismatch` rather than silently accepted.

## What it must never be read as

* Not the production verifier. `tests/a15_acceptance/validator.py` validates a canonical *synthetic*
  candidate graph with `fixture-signature:` exclusions; it is never imported here and its green
  result is never live evidence.
* Not authority. A retained `verdict: GO` or `state: complete` is recorded as an observation and is
  never consulted to derive readiness.
* Not a decision. The report is structurally incapable of a readiness claim: `release_ready`,
  `authoritative` and `production_verifier` are `false`, `gate_state` is `design_only`, and two
  guards raise if any code path tried otherwise.

The report also lists `missing_evidence` and the seven `missing_verifier_contracts` gaps that would have to
be closed — under a governance decision this slice is not authorized to make — before retained artifacts
could be promoted to current executed gate results. A release-decision producer exists in
`mycelium_a15_release_decision.py`; the gaps are that retained artifacts are not signed result receipts in
a content-addressed graph and that no production trust registry is pinned, not that no verifier exists.
