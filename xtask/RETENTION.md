# Retention inspection and fixture rehearsal

`xtask retention` is offline release tooling for CL-L12-04 / B-REL-10. It does not launch commands, use the network, collect live caches or infer that a release is qualified.

## Manifest (schema 1)

A JSON document has exactly `schema`, `preserve` (nonempty closure IDs), and `closures`. Each closure has `id`, `kind`, nonempty `members`, and optional `requires` IDs. Each member has a literal relative `path` and optional lowercase SHA-256 pin for a regular file. Unknown fields, duplicate IDs/members, unknown references, unsafe/noncanonical paths and malformed pins are errors. Reachable dependency cycles are safe: each closure is visited once.

The transitive closure of `preserve` must cover all six kinds: `active`, `native`, `npm`, `model`, `continuity`, `rollback`. A declared but unreachable kind does not satisfy that rule. Directory members protect their entire subtree and must contain at least one regular file. Without a pin, verification checks existence/type/nonempty tree, not an independent inventory of every expected file. Pin individual required files when completeness matters.

The data-only manifest builder in `tests/retention.rs::manifest_json` is an executable example (spec fixture, not product/model evidence).

## Commands

```text
xtask retention plan --manifest retention.json --root /absolute/private/store --candidates candidates.txt
xtask retention verify --manifest retention.json --root /absolute/private/store
xtask retention cleanup --manifest retention.json --root /absolute/private/store --candidates candidates.txt --preflight
```

`candidates.txt` has one literal relative path per line; empty and comment (`#`) lines are ignored. Paths are never globbed. `plan` is read-only and reports logical file bytes, not APFS reclaimed disk space. A candidate equal to, above or below a protected member is refused. Duplicates, overlapping candidates, symlinks (including ancestors/descendants), special files, missing candidates and retention/preflight control-file candidates are refused. Case comparisons conservatively protect case aliases, including on case-sensitive hosts.

`cleanup` is **only for an exclusively owned disposable fixture**, never an active workspace, installed app, production data, model cache or another lane's tree. Before creating the fixture, resolve its intended scratch parent to an absolute path without symlink ancestors (macOS `/var` itself is a symlink). The root must have a regular `.retention-fixture` file containing the SHA-256 of the exact manifest bytes. The marker is an accident-prevention check, not a security boundary or proof that the tree is disposable. Do not run against concurrent writers: path inspection/deletion is not an atomic filesystem transaction.

Cleanup validates every protected member and the entire plan before removing anything. An input/plan defect removes nothing. With `--preflight`, it evaluates the real `Preflight` implementation on `xtask/prerequisites.json` and `xtask/quarantine.json` using empty PATH/environment, requires every selected group to be RUN, removes only the accepted candidates, verifies closures again, then re-evaluates preflight. A changed readiness result is a finding even if the manifest omitted the dependency. `RUN` denotes offline readiness only; no group command executes. Without `--preflight`, no before/after preflight claim is made.

Exit codes: **0** clean, **1** findings, **2** input or filesystem error. Cleanup is not transactional: an I/O failure after earlier removals can leave a partially cleaned disposable fixture; it propagates as an error, never success. There is no automatic rollback or retry.

## Qualification boundary

Tests exercise dry-run immutability, all six closures, transitive protection, exact logical bytes, marker binding, missing/tampered members, unsafe paths, no partial deletion on a refused plan, offline before/after readiness and the historical undeclared native-cache deletion failure. This establishes fixture cleanup, not live cache retention, device acceptance, final no-Python inventory or release acceptance. Those need independently bound candidate evidence.
