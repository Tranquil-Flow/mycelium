# SPDX-License-Identifier: AGPL-3.0-only
# L0 source census / contract review

Source: `f5bfb70981569c79cb873afe8e0c5be20342db7e`, read with Git object commands
only. No command from the old worktree was executed. Census generation was a
one-off external Ruby reader; the new repository has no executable generator
or dependency on that reader.

## Static closure

- `scripts/build_qwen_live_route.py:109-128`: initial package/module sets.
- `scripts/build_qwen_live_route.py:770-798`: absolute-import head expansion to
  root module or all Python files in the package; exclude test heads.
- `scripts/build_a13_macos.py:248-263`: add compiler script and all A13 macOS
  Python sources, then stage source data.
- The read-only lexical import census strips string blocks, scans imports and
  expands those same heads to a fixed point. Result: 725 paths, including 188
  scripts and 81 root modules, matching `V02-PLAN-CHECKPOINT.md:8`.
- This is a static source-data census, not AST execution, import reachability or
  a package-resource audit. Exact path/hash/size records are frozen in baseline.

Every source citation above is **@ f5bfb709**. A6's 1,336 entries are read from
`artifacts/a6/source-manifest-v1.json:1 @ f5bfb709` and reconciled independently;
its historical `base_commit` field is not substituted for the census source SHA.

## Contract inventory verification performed during extraction

Read `scripts/contract_audit.py:1-190 @ f5bfb709` and
`scripts/contract_registry.py:15-666 @ f5bfb709`. Without executing Python:

1. Parsed all literal `ContractSpec` rows (87).
2. Required exact fixture filename/protocol pairs and owner-source sets against
   `contracts/contract-manifest.v1.json:1 @ f5bfb709`.
3. Required exact compatibility-fixture directory namespace from the Git tree.
4. Verified SHA-256 and byte length for every fixture and owner-source pin using
   the pinned Git blob; required fixture `protocol` to match its owner.
5. Wrote crosswalk only after these comparisons succeeded.

This is a **read-only pin/namespace crosscheck**, not a claim that the existing
Python contract auditor ran. Its strict duplicate-key/safe-path behaviour becomes
an explicit Rust replacement obligation; no executable Python is copied.
The Rust suite independently compares the new crosswalk to the original parsed
manifest and every A6 path/hash/size to the pinned source census.

## Reviewed boundary examples

- `physical_inference_node.py` maps to `node::worker`, not harness retirement.
- `mycelium_live/local_preparer.py:1833,2269,2410,2574` references
  `bind_operator_plan_to_seed.py`, `stage_live_route.py`, `build_qwen_live_route.py`;
  these map to planner/artifact runtime destinations, not generic audit tools.
- `mycelium_live/model_capacity.py:27` imports `assemble_m17_swarm_evidence`;
  its signed-capacity-generation behaviour maps to `planner::capacity`.
- `scripts/runtime_archive_bootstrap.py:1-5` owns verified-tree lifetime/cleanup;
  map to `artifacts::staging`, preserving identity/cleanup checks while replacing
  source staging. Interpreter selection is not a v0.2 runtime dependency.
- `mycelium_reference_oracle/qwen_parity.py:1-20` is a comparison reader and maps
  to Rust test support. External-oracle disposition applies to the other four
  numerical execution/report surfaces, not this comparison behaviour.

All boundary-example citations are **@ f5bfb709**. A-number names alone never
justify retirement. All test files, helper modules, fixture data and quarantine
records remain mapped; replacement and retirement strength is unproven.

## Dependency receipt

All dependencies were already cached; all cargo operations were offline and no
models, toolchains or installers were acquired. Direct: serde 1.0.229 (derive),
serde_json 1.0.154 (aligned to the merged L4 pin), both MIT OR Apache-2.0.
Resolved obligations transitive packages:

- serde_core / serde_derive 1.0.229, proc-macro2 1.0.107, quote 1.0.47,
  syn 3.0.9, itoa 1.0.18: MIT OR Apache-2.0.
- memchr 2.8.3: Unlicense OR MIT (MIT admitted).
- unicode-ident 1.0.27: (MIT OR Apache-2.0) AND Unicode-3.0.
- zmij 1.0.23: MIT.

Licences read from actual offline Cargo metadata. Full versions/checksums are in
`resolved-workspace.lock`; the integrator alone updates the root lockfile.
