<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Released-package disposition review — L0 inventory, not release approval

## Identity and scope

- Accepted source: `f4e366e98a1f705dfaf182897044500d290dbd7f`.
- Published ZIP SHA-256: `fce4743a7f3a2453f6621753ed3892a6cefaddf52e7a3e98fd79ba51f8930025`.
- Exact census: `test-support/obligations/data/package-census.json`.
- Reviewed dispositions: `test-support/obligations/data/package-dispositions.json`.
- Authority: V02-PLAN §§1.3, 3, 4.6, 5/L0. Engineering review is delegated L0 inventory work; it does not assert owner approval to delete history or retire user-visible behavior.
- The round2 `package-reconciliation.json` is preserved as historical RED evidence. This review supersedes only its unexplained-package-path/opaque-container findings, not its contract/A6 rebinding gaps.

All 637 previous gaps have exact members in 40 reviewed families: **8 Rust destinations, 361 native-reused identities, 229 representation retirements, 39 retained data records**. Including the earlier source/web entries, the ZIP has 1,552 entries: 182 structural directories and 1,370 dispositioned files/links. Every non-directory has exactly one disposition. Across ZIP and nested archives, 431 families cover 4,001 non-directory entries.

A family prefix/kind/suffix rule is a constraint on its enumerated members, **not** permission to admit another matching path. Family records contain destination, component, reason, reviewer, authority/source citation and measured evidence pins. Members have exact path, byte length and SHA-256; symlinks pin link bytes, not dereferenced content. Source crosswalks also bind accepted-source digest and the existing obligation ID.

## Review decisions and boundaries

1. **Interpreter and wheel representation retirement:** CPython framework, 55 stdlib ABI extensions, named wheel ABI bindings, wheel metadata, Python typing stubs, bootstrap tables and bytecode are retired under §1.3. Product behavior remains assigned to Rust owners; no supported backend/provider, API, consent default or old test is retired by this decision. `numpy` ABI bindings belong to `backend-cpu::qwen2`, not the MLX implementation.
2. **Native dependencies are not Python baggage:** the released Iroh sidecar and cloudflared identities remain explicit, as do MLX dylib/metallib and all 357 enumerated native SDK headers. MLX headers may matter for build/JIT paths and are not blanket-deleted. The selected v0.2 MLX 0.32.4 inputs are **not byte-identical** to the released 0.31.1 wheel. “native-reused” preserves native component responsibilities and the exact oracle identities; it does not certify these bytes as the final Rust candidate or approve an ABI/version substitution. L8/L11 retain numerical, JIT/runtime placement, minimum-OS and relocation qualification.
3. **Seven interpreter-linked dylib copies:** `package-native-import-review.json` records actual load commands for 103 Mach-O images. Consumers of libcrypto/libssl, libexpat, liblzma, libmpdec, libsqlite3 and libzstd are the named CPython extensions (and libssl → libcrypto). This supports retiring these exact bundled copies, not eliminating TLS/SQLite/compression/XML behavior. Static import inspection is not a dynamic-loading or runtime qualification claim.
4. **Desktop/installer behavior remains:** the app executable, resource probe, Quit bridge, Info.plist and four installer envelope files have Rust destinations. Finalizer references were corrected to `scripts/finalize_a13_macos.py` at the accepted commit; the initial builder reference alone did not produce the installer/checksum envelope. Old ad-hoc signature bytes and PyInstaller crosslinks are retired, not the signing policy or link target obligations.
5. **Retained data has provenance:** CA trust bundle, icon, SDK documentation, exact dependency licenses and release/compiler/native identity records remain pinned. Historical notices remain available; applicable notices follow reused dependencies. This neither updates TLS trust nor claims the future Rust/native SBOM is complete.
6. **Web assets remain unchanged:** all eight shipped HTML/CSS/JS resources retain exact accepted-source bytes and L10/L11 service/webview obligations. This is not permission for a frontend rewrite.

## Embedded archive census

The executable was read as data, never launched. The bounded external table decoder reads the PyInstaller CArchive and PYZ index without deserializing/executing code objects. It hashes decompressed member bytes. System `unzip` independently verifies the outer ZIP and each file/link; a fresh table extraction is compared to every committed embedded row.

| Container scope | Exact entries | Disposition boundary |
|---|---:|---|
| CArchive | 13 | App `__main__` has its source obligation; bootstrap and PYZ container representation have separate retirement rows |
| PYZ | 2,463 | Every module/package/namespace individually listed; source modules use existing obligations; dependency/stdlib representations are explicit member lists |
| base_library.zip | 155 | Every stdlib bytecode member enumerated independently of the container |
| **Embedded total** | **2,631** | No opaque container disposition grants coverage to children |

The embedded source mapping identifies 441 unique source objects, including five test-support sources absent from the loose runtime/web set: `tests/__init__.py`, `tests/a10_acceptance/__init__.py`, `tests/a10_acceptance/benchmark_amendment.py`, `tests/a5_acceptance/__init__.py`, `tests/a5_acceptance/materiality_harness.py`. All five map to `test-support::contracts`; none is silently retired as a dependency. The frozen `sitecustomize` module is separately assigned to `host::process` for startup-behavior review rather than assumed to be harmless stdlib debris. The census pins 739 accepted Git objects, including 738 distinct mapped source objects plus additional evidence identity. Source hashes authenticate the cited source, **not equivalence of compiled Python bytecode to that source**.

## Validator and discrimination

The original acceptance RED was a real assertion: 637 unexplained paths versus zero. The resumed WIP also admitted syntactically valid but false evidence hashes and false embedded-source hashes. `package_evidence.rs` demonstrates both assertion REDs before the fix. `validate_package` now binds evidence hash/size/path to a census/source object and embedded-source hash to the accepted object catalogue.

The Rust tests require the complete original ZIP path set, zero unexplained paths, all 637 old gaps individually required, and all embedded members covered independently of containers. Negative controls add a same-shape header, a same-shape embedded dependency and a new native binary; all remain unexplained. They also reject missing/duplicate members, mismatched hashes/sizes, bad crosswalk destinations, missing review/provenance, wildcard rules, unknown schema fields, empty census, invalid container/link identities, and hiding native runtime entries in the header family. The external verifier additionally rejects a corrupt whole-ZIP digest.

## Remaining L0 boundary

Package-path coverage is closed. The final contract-owner/A6 manifests still differ from the provisional `f5bfb709` crosswalk baseline. Final endpoint/profile/old-suite-history rebinding remains separate work: this package overlay neither rewrites those historical identities nor claims they passed the final release oracle. Therefore FOUNDATION-EXIT L0.1/L0.2 remain PARTIAL overall, with the 637-path and embedded-census subclauses closed. No fixture was recaptured, old test removed, product behavior approved for deletion, or overall foundation exit granted.
