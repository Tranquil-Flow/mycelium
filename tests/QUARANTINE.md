# Test flake quarantine register

**Scope.** PLAN v2 §2.1: "a test that fails under L2 but passes 3/3 in isolation goes to
`tests/QUARANTINE.md` (the test id, first seen, suspected cause), is marked `flaky` and is
excluded from L2. A lane is dispatched to fix it within the milestone. There is never an r12."

This file is the register. It is the **only** place that may assign the `flaky` marker, and it
is **empty**. Nothing is registered here today, so nothing carries `flaky` and the L2 selection
(`not scaffold_red and not flaky and not emulation and not physical`) currently excludes no test
for flakiness.

## The 3/3 procedure (do not shortcut it)

A test may be entered in the register only when all three steps are done, in order, and each one
left a receipt:

1. **Seen red under L2.** The test failed in an L2 run whose command and run id are known.
   Receipt: the run id plus the failing test id as pytest reported it.
2. **Three isolation receipts.** The exact test id is re-run alone, three separate times, on the
   same bytes, on a quiet host (1-minute load average below 8), and it passes three times out of
   three. Receipt: three commands with their outcomes, each `1 passed`.
3. **Cause named, honestly.** The row below records the suspected cause. "Unknown" is an
   acceptable cause; a guess that has not been checked is not.

Three attempts, then stop: a test that does not pass 3/3 in isolation is **not** flaky. It is a
real red, it stays red, and it is fixed - it is not quarantined. A test that fails under L2 with a
different test in the same file may instead be a test-isolation defect in that file, which is also
fixed, not quarantined.

**No blanket skips.** The `flaky` marker is not a skip, not an `xfail`, and not a way to make a
failing suite green. The item still collects. Quarantine changes which runs select it, and it
requires the three receipts above.

## Register

| Test id | First seen (L2 run id, date) | 3/3 isolation receipts | Suspected cause | Fix lane |
| --- | --- | --- | --- | --- |
| _(none)_ | - | - | - | - |
