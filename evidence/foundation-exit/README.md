<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Foundation-exit evidence — identity admitted, exit still BLOCKED

This data-only snapshot binds the accepted released v0.1.4 oracle and the actual
L8 native build inputs used by integration preview 3. It does not enable a live
runner or claim native/package/physical qualification. No Python code or runtime
was added to this repository, and no package/native code was executed to acquire
this snapshot.

## Inputs and use

From the repository root, set:

```sh
export MYCELIUM_ORACLE_MANIFEST="$PWD/evidence/foundation-exit/oracle-provenance.json"
export MYCELIUM_NATIVE_INPUT_MANIFEST="$PWD/evidence/foundation-exit/native-inputs.json"
```

Use the lane's host-admission guard before **every** Cargo command (no demo busy
marker, minimum free disk, nice 19, offline/locked, two jobs). The guarded command
`cargo run --offline --locked -j 2 -p xtask -- preflight` admits all six selections,
including `native-adapters: RUN` and `oracle-journeys: RUN`. Exact before/after
output and hashes are preserved in `preflight-readiness.json`.

**RUN is discovery only, not PASS.** `xtask/src/preflight.rs::detect` checks that
an env-file is a regular file; it does not parse a manifest or verify hashes.
`xtask/README.md:147-155` explicitly describes that boundary. These JSON envelope
schemas are evidence, not a claim of an existing shared parser. No L12 crate was
edited. The old README recommendation to keep inputs external is superseded for
these non-secret data by BACKLOG 10's explicit committed-manifest requirement.

- `oracle-provenance.json`: accepted source commit/tree, exact certified
  executable, published ZIP, READY and release-receipt paths/digests, original
  journey fixture identities, and separate accepted-successor replay receipts.
- `oracle-target.json`: the runner's **exact four-field** `Target` schema. If an
  oracle run is separately authorized, pass this to `--target`, not the richer
  provenance envelope (`Target` denies unknown fields). Setting an environment
  variable does not run or configure a journey.
- `native-inputs.json` (**round 5: switched to L8's macOS-14 rebuild, pin set `candidate-minos-14.0`**):
  `MLXC_INCLUDE_DIR` (all 44 headers, exact tree hash) / `MLXC_LIB_DIR`
  (`~/Projects/mycelium-native-macos14/install/lib`), mlx-c/MLX revisions, source archives, vendored-tree
  digests, build script/provenance/log, licences, the three dylibs (including `libjaccl`, which libmlx does
  not reference at startup) and the metallib with bytes/digests/otool minos 14.0/install names/rpaths/
  metallib triple `air64_v26-apple-macosx14.0.0`, fixtures, L8's exactness suite on these libs and L8's
  macOS 15.6.1 load proof (cited, not re-run here). All four files match `backend-mlx/src/pins.rs`
  (included by `build.rs`) and `crates/desktop/package-pins.json`. Independent re-verification:
  `FOUNDATION-EXIT-round5/verify-candidate.{rb,log}` (79 checks). The previous manifest is kept
  byte-identical as `native-inputs.minos-26.5.1.superseded.json` with
  `native-inputs.minos-26.5.1.superseded.NOTE.md` (libs record minos 26.5.1 and cannot load on macOS 15).
- `package-reconciliation.json`: **all 1,552 entries** from the actual published
  ZIP, not a guessed source-tree list: 182 directories, 725 runtime-source files
  plus eight web resources mapped to existing dispositions and byte-equal to
  accepted source, and **637 individually listed unexplained package paths**.
  `unexplained_paths` is the complete actionable list. No wildcard third-party
  retirement was invented. Embedded PYZ/base-library module closure remains open.
- `seal.json`: exact-byte SHA-256 bindings for this data snapshot and its external
  acquisition/verification receipts. It is content binding, not a signature.

## Qualification and owner boundaries

- **L0:** adjudicate each unexplained package path and embedded archive module;
  rebind provisional baseline/static/contract-owner/A6 crosswalk and external
  test/history retention to the final release. Actual runtime source path set is
  identical to the provisional 725-path static set, but final contract and A6
  manifest **bytes differ**. Path equality does not close that provenance review.
- **L12/L4:** reconcile descriptive preflight commands with real runner/Metal
  invocation before scheduling. Add owner-agreed content/schema verification and
  fresh-worktree admitted-input setup; metadata presence is not authentication.
- **L8/L11/L12:** pin exact candidate toolchain and packaged executable/resources;
  qualify relocation, minimum OS, install/upgrade/rollback and native IO on the
  same candidate. No historical L8 Rust toolchain pin was available in this
  snapshot; `native-inputs.json` keeps that gap explicit. Existing L8 receipts are
  historical component evidence, not executions of this merged candidate.
- **L3/owner:** full joined-member/two-host HTTPS/model-backed HTTP corpus remains
  blocked. Preserved 20+33 observations are the prior released-oracle slices, not
  a fresh round-2 run or a full corpus. Golden capture identities are not relabelled.

Acquisition and independent verification are archived outside the product/test
path under program `runs/v2/v02/FOUNDATION-EXIT-round2/`. The verifier rehashed 96
unique referenced inputs, checked exact ZIP enumeration/header/build pins and
runner keys, and rejected an in-memory corrupt digest. No external evidence was
modified. All required absent qualification stays BLOCKED despite readiness RUN.
