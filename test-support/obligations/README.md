# Obligations registry (L0, preserved history + additive accepted-source bindings)

## Accepted static-closure identity successor (FX r8)

`static_source` binds the preserved registry to accepted source
`f4e366e98a1f705dfaf182897044500d290dbd7f` without rewriting provisional
history. `accepted-static-source-manifest.json` pins the complete packaged
source/resource population and its profile-context/derivation inputs;
`accepted-static-binding.json` binds each mapped path to its original path ID,
obligation ID, destination and disposition. These are inert source-inventory
metadata, not new runtime fixtures or behavioral expected values.

`static_source::validate_binding` checks exact accepted input identity, preserved
history, package-catalogue membership **and bytes**, exact overlap pins with the
already accepted A6/contract/package inputs, and a complete registry crosswalk.
`package_gate` now invokes this structural check alongside its existing accepted
contract check. It does not replace actual source-byte authentication.

After canonical host/material admission:

```sh
cargo test --offline --locked -j 2 -p obligations --all-targets --no-fail-fast
cargo run --offline --locked -j 2 -p obligations --example static_gate -- /path/to/read-only/oracle-git
```

The real Rust `static_gate` authenticates every pinned local history file before
reading each distinct immutable Git source object exactly once and checking its
length and SHA-256. Optional binding, manifest and package-catalogue JSON inputs
provide fail-closed negative controls. Metadata corruption is refused before any
history/bulk reads; byte corruption and missing objects propagate as errors.
Git and the host SHA-256 utility are synchronously reaped; no oracle application
or Python executes in this gate or the tests.

The accepted population is **725 packaged source paths + 8 resources**, with
11 profile-context source paths (overlapping those populations). The crosswalk
contains **735 unique mapped paths**; adding derivation inputs gives **737 unique
source objects / 13,990,599 bytes**, plus **13 preserved local history files**.
Membership matches the accepted package catalogue exactly. Relative to the
provisional closure, membership is unchanged but **11 source pins changed**.
All 14 profile contexts retain `UNPROVEN` semantic qualification.

**Claim boundary:** zero unexplained paths in the accepted static source/resource
population only. Profile source-context pins preserve explicit `UNPROVEN` states:
endpoint/profile semantics, remaining old-suite rebinding, behavioral replacement,
native/device qualification and foundation GO remain open. All original seals,
counts and dispositions remain unchanged; zero test retirements.

## Accepted A6 identity successor (FX r7)

`data/accepted-a6-source-manifest.json` retains the **exact** final Git-object bytes
at `f4e366e98a1f705dfaf182897044500d290dbd7f:artifacts/a6/source-manifest-v1.json`,
SHA-256 `95b11297bac9d6384bdfb8cbae98c60b91e265b289f6f1f385f9190208a44c9f`.
Its 1,347 original pins keep their literal `sha256:` prefix; the Rust adapter
validates that schema before supplying the digest to the existing byte verifier.
The original 1,336-entry manifest, registry, fixtures, counts and seals are not
rewritten or relabelled as final-source captures.

`accepted-a6-binding.json` adds exactly ten previously unmapped paths and ten
crosswalk groups: nine test modules with **28 test bodies**, plus the zero-test
`inflight_stop_support.py` helper. The nine module body counts are:

| Module (under `tests/`) | Bodies |
|---|---:|
| `a13_macos/test_inflight_chat_teardown.py` | 1 |
| `a13_macos/test_inflight_progress_retirement.py` | 1 |
| `a13_macos/test_inflight_progress_stop.py` | 1 |
| `a13_macos/test_inflight_route_stop.py` | 4 |
| `a13_macos/test_inflight_stop_order.py` | 1 |
| `a13_macos/test_registry_stop_drain.py` | 2 |
| `a13_macos/test_serving_discovery_failure.py` | 9 |
| `a13_macos/test_token_progress_retirement.py` | 8 |
| `live/test_completion_cleanup_stop.py` | 1 |

There are **14 additive obligations**: ten new-path obligations and four named
changed-existing-group obligations. The latter retain their existing `T:` IDs,
full historical group membership and a hash-bound TB4 baseline row/table identity:
worker replacement adds one body (7→8); governed-completion snapshot refresh,
local-token authority and token-path owner change helper context. Their old 19
and new 20 test bodies are separately enumerated and byte-bound. Unchanged test
body bytes do **not** prove equivalence when helper context changed. The helper's
historical `python_only` disposition does not retire the successor obligation.
The old 1,337 groups/10,334 definitions remain immutable; no counter is reset.

The A6 path union is 1,336 old + 11 added, zero removed. The eleventh addition,
`mycelium_live/command_controller.py`, already has a path obligation; it is not
invented as another group. `a6::validate_binding` checks the exact immutable
successor, whole historical registry preservation and structural union through
the existing exhaustive validator. It is **not** source-byte authentication.

After canonical host/material admission:

```sh
cargo test --offline --locked -j 2 -p obligations
cargo test --offline --locked -j 2 -p obligations --example a6_gate
cargo run --offline --locked -j 2 -p obligations --example a6_gate -- /path/to/read-only/oracle-git
```

The Rust CLI authenticates the actual final manifest, all 1,347 A6 source objects,
and the qualified 204-contract-object union with 51 overlaps: **1,500 distinct
final objects**, each source-union member read/hashed once. It also authenticates
four historical source objects, the 19 old/48 current reviewed body spans and 13
unchanged local history/contract files. The actual contract manifest is verified
separately; corruption controls fail before the bulk Git reads. Optional binding
and history JSON arguments permit real CLI refusal tests without modifying the
oracle. Git and the existing host SHA-256 utility are the only subprocess helpers.
No Python is imported or executed by any consumer or test; external pinned AST
extraction produced metadata only, never runtime expected results.

**Claim boundary:** zero unmapped paths in this final A6 population, not test
retirement, Rust behavioral equivalence, native cleanup, fixture recapture, full
old-suite/static-closure or endpoint/profile rebinding, or foundation GO. All new
body-context obligations remain `unproven`; TB enumeration is not proof. Next
bounded source seam is final static-closure and endpoint/profile rebinding.

## Accepted contract authority (FX r6)

`data/accepted-contract-manifest.json` is the exact accepted Git object at
`f4e366e98a1f705dfaf182897044500d290dbd7f:contracts/contract-manifest.v1.json`
(SHA-256 `dde648d2af8cc77e0a697cac820a3ef7ea2a043537ca542106038467cc19aa4e`).
`accepted-contract-binding.json` rebinds all 87 protocol rows to that authority:
14 rows have changed owner pins, while all original fixture pins, destination IDs
and obligation IDs are retained. The original files below remain unchanged.

`contracts::validate_binding` requires exact protocol/owner/fixture membership,
final source/manifest identity, historical destinations and obligations, and an
explicit path obligation for every owner and fixture. The ordinary `validate`
also checks historical contract metadata against its exact historical manifest;
metadata is no longer merely a well-formed unexamined JSON field. Arbitrary
caller-supplied source snapshots remain structural inventories, not authenticated
source authorities.

After the canonical host/material guard, run:

```sh
cargo test --offline --locked -j 2 -p obligations
cargo run --offline --locked -j 2 -p obligations --example contract_gate -- /path/to/read-only/oracle-git
cargo run --offline --locked -j 2 -p obligations --example package_gate
```

The **Rust** `contract_gate` reads the pinned Git objects as inert bytes and
checks actual byte length/SHA-256 using `/usr/bin/shasum` (a host tool prerequisite;
no new registry dependency or hand-written cryptography). It rehashes the
manifest plus all 204 distinct contract source objects: 117 owners and 87 fixture
files. 88 pins are independently equal to the completed package census and are
reused by exact identity. All helper processes are synchronously reaped. It never
imports Python, executes the application, or rebuilds native/model assets.

`package_gate` additionally enforces the accepted contract **structural** binding;
its package census/embedded-member evidence is unchanged. Actual source-byte
acceptance requires `contract_gate`, not the structural gate alone.

**Not closed by r6:** final static-closure, A6, endpoint/profile and full old-suite
history rebinding; test retirement/replacement strength; numerical/native/device
qualification; foundation exit. A6 has ten paths absent from the preserved source
registry and needs an explicit additive test-history review. TB-4 group enumeration
and source presence are not behavioural proof. No historical fixture was refreshed.

## Preserved first-slice provenance

This crate is discovery/test infrastructure, not a product port. It freezes source
`f5bfb70981569c79cb873afe8e0c5be20342db7e`, **not** an accepted v0.1.4 release.
No historical application, Python audit, old tests, device, relay or model was run.

## Coverage and data

| File | Meaning |
|---|---|
| `data/baseline.json` | Source SHA, SHA-256/size pins, exact supplied path lists, closure derivation and A6 set differences. Tests never consult the old worktree. |
| `data/paths.json` | One exact row per path; no wildcard rows. `rust`, `external oracle only`, or structurally reviewed `retire`. |
| `data/destinations.json` | Proposed `crate::module` vocabulary with plan/lane ownership. A declaration is not a claim that the product crate exists. |
| `data/obligations.json` | Stable behaviour IDs, Rust owner, primary tier and **planned** proof state. |
| `data/contracts.json` | All 87 protocol owners, original fixture/source pins, owning Rust crate and proof obligation. |
| `data/test-groups.json` | Every file under `tests/`, plus root test files; definition names/line citations, import evidence, primary destination/tier, support-file roles. |
| `data/escapes.json` | Exact ten plan §7 recipes/observations and historical expressions (including `^`) plus resolved SHAs. **Inventory only**, never a reproduced RED claim. |
| `data/supported-cells.json` | Registered models versus reachable schema/backend profiles, source-supported macOS admission and explicit unknowns. |
| `data/a6-source-manifest.json`, `data/contract-manifest.json` | Original parsed manifests retained as independent crosswalk expectations. No source-byte equivalence between Python and Rust is implied. |
| `data/counts.json` | Census totals, not test execution or release qualification. |

There are 725 packaged-source paths, 1,336 A6 entries, and 1,337 old test/support
files (1,239 under `tests/`, 98 at root). Their union with app assets and contract
owners/fixtures has 2,469 distinct paths: **2,397 Rust destinations, 72 external
oracle-only, zero retirements**. Within the 725 closure: **721 Rust, 4 external**.
The external numerical execution/report generators remain outside the product;
`qwen_parity.py` comparison/fixture-reader behaviour maps to Rust test support.
All other oracle-only paths are retained historical documentation/evidence, not
permission to discard unique requirements. Every row still has a replacement
obligation. Existing frontend assets are retained, not rewritten in Rust.

Test-group rows retain 10,334 statically discovered test definitions in 1,185
files, plus 152 support/fixture files. These are **not pytest-collected or
parameter-expanded IDs**. No test retirement, quarantine waiver, historical
failure adjudication or mutation-verification claim is made. Primary tiers are
proposed migration destinations; mixed files must preserve their additional
platform/integration obligations when split. Names and imported sources support
this first mapping; product lanes/L12 must refine it before replacing tests.

## Validation API

`checked_in()` uses serde to load the embedded data; `validate(&registry,
&snapshot)` checks a caller-supplied census. A later final-baseline census with a
new path fails closed until it receives a mapping. Errors have stable codes and
subjects, and are accumulated rather than silently ignored. Crosswalk metadata
(test definition names, pins and tier) is retained in `Crosswalk::evidence`.

The validator rejects unmapped/duplicate paths, duplicate IDs across registries,
unknown destinations or obligations, missing contract/test rows, malformed pins,
source mismatch, empty retirement reason/replacement/reviewer, missing mandatory
incident IDs and unsupported incident proof claims. A reviewer string is only a
structural requirement: human approval and replacement-strength evidence remain
separate release gates. Six checked-in negative fixtures cover the five required
rule classes; both missing retirement fields are tested separately.

From the workspace root, **only after the release host is admitted**:

```sh
cargo test --offline -j 1 -p obligations -- --nocapture
cargo clippy --offline -j 1 -p obligations --all-targets -- -D warnings
cargo fmt --check -p obligations
CARGO_BUILD_JOBS=1 cargo xtask lint
```

The release owner's busy-marker/when-quiet guard must precede **every** cargo
command, including checks/formatting. No Python, generator, Ruby or old-worktree
lookup is part of these commands. Initial snapshot extraction was a disposable
external read-only Git/Ruby census, never an executable dependency of this repo.
The integrator owns the root lockfile; this lane supplies its resolved lock as
`evidence/resolved-workspace.lock` rather than changing that shared path.

## Scope boundaries / next slice

- The static closure is derived from `scripts/build_qwen_live_route.py:109-128,
  770-798` and `scripts/build_a13_macos.py:248-263 @ f5bfb709`. A lexical import
  census (without Python execution) matches the prior 725-file checkpoint,
  including 188 script paths and 81 root modules. Source SHA pins every input.
  A6 is a **different changed-source set**, not the package inventory.
- Final executable/resources/dynamic-import/public-endpoint reconciliation,
  accepted package/device receipts and critical-fix delta comparison are pending
  the parent's final v0.1.4 pin. Do not use this census as final-package proof.
- Five registered A13 models and four GUI choices are distinct. Qwen3.5
  hybrid/MoE and legacy GPT2 schemas remain reachable contract obligations, not
  silently removed or falsely advertised as registered A13 models.
- Lowest exact admitted macOS is **15.6.1**; the admitted set is **15.6.1 and
  26.5.1**, arm64. This is not an arbitrary-version interval or proof of the
  final bundle's Metal minimum. Final `LSMinimumSystemVersion` remains unknown.
- Source `fast_mode.document` defaults to **normal/off**, while historical
  pipeline prose calls fast/default. Preserve source/saved settings pending
  final-pin comparison; do not manufacture a fresh-default grant or mode.
- F16 correction/accepted baseline, measured p99s, retirement approvals and
  replacement tests are not earned by these registry tests.
