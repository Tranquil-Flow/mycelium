# Desktop shell (L11, preparatory)

Tauri 2.12.0 / Wry 0.57.0 macOS shell, not a release or a working inference app.
Only `crates/desktop/**` is owned by this lane. The integration preview's unified
offline lock is authoritative; no private lock/source mirror is needed.
Do not integrate into main before the foundation exit gate.

## Implemented

- The eight `mycelium_a13_macos/web` files are byte-identical to oracle
  `f4e366e98a1f705dfaf182897044500d290dbd7f`; `assets.provenance.json` pins each.
  No npm/Vite build is required for this static UI. The icon is from the same
  oracle's mobile Tauri shell. L10 has identical copies; deduplicate at integration.
- Native private custom-protocol requests and async L10 callback seam; see
  [SECURITY.md](SECURITY.md). No listener or token bootstrap. External HTTP(S)
  links go to the system browser without giving another document native authority.
- Explicit CSP/navigation/popup containment, unchanged local-chat `_blank` routing,
  and a real WebKit adversarial matrix. The smoke records external-browser intent
  at an injected OS seam and does not launch the user's browser.
- Concrete bounded `services::ServiceCore`/`ServiceCleanup` adapters sharing one
  lifetime and callback drain; `shell::Core`/`Reply` remain compatible reexports.
- One monotonic bounded cleanup state machine; explicit AppleEvent handler,
  native readiness barrier, menu Quit/Cmd-Q, reopen, window-close and web Quit.
- OS admission preserves `app.py:84–90,124–128`: Apple Silicon, macOS **15.6.1
  exactly**, or numeric two/three-component **26.0+**. Finder metadata uses
  15.6.1, not a claim that all intervening releases are supported.
- Relocatable native payload admission, streamed SHA-256/size checks, missing and
  corrupt library/metallib refusals, traversal/duplicate/symlink refusals, exclusive
  staging and an inside-out ad-hoc signing plan. No signing runs implicitly.

`run_packaged` probes the real host and admits native bytes before calling the
L6/L8/L10 core factory. This lane does not supply that factory, a trusted production
native manifest, or a release executable. `run_development` exists only with the
`dev-shell` feature and refuses release-profile execution. Its explicitly named
binary requires `--isolated-dev`, uses a nonpersistent WebKit store, returns 503
`core_not_integrated` for core APIs and fakes empty cleanup. No models, keys,
consent, identities, installed apps or real service state are touched.

## Commands

Always use the owner's host/disk guard before **each** cargo invocation, nice 19,
`-j 2`, and a private target directory. Examples omit that external wrapper:

```text
cargo test --offline --locked -j 2 -p desktop
cargo clippy --offline --locked -j 2 -p desktop --all-targets --features dev-shell -- -D warnings
cargo run --offline --locked -j 2 -p xtask -- lint
cargo build --offline --locked -j 2 -p desktop --features dev-shell --bin mycelium-desktop-dev
```

Explicit GUI gate (only when authorized; briefly launches its own relocated dev
`.app`, then targets only the owned child PID with a real AppleEvent and reaps it):

```text
cargo test --offline --locked -j 2 -p desktop --features dev-shell --test native_smoke -- --ignored --nocapture --test-threads=1
```

`native_smoke` is intentionally opt-in, never counted as run by the default test
command. Its registration proof uses readiness events, not an arbitrary sleep.
The Rust harness embeds only a development DOM/fetch probe, not a JS test runner.
It does not exercise Finder double-click, quarantine, Gatekeeper or Metal.

### Consumer-owned development probes

`shell::run_development_with_probe(core, cleanup, cleanup_budget, script: String)`
is available only with `dev-shell` **and** debug assertions. It lets a composition
owner inject chat/Stop probes without editing frozen UI assets. It uses the same
native channel and cleanup, logs `L11_*` title receipts, emits
`l11-title-recorded`/navigation/popup events, and records the external-browser seam
instead of opening the user's browser. It starts unfocused and owns no service
credentials or model fixtures. The caller owns all service setup and its script.

The inert binary accepts `--isolated-dev --probe-script FILE --service-capacity N`
only in debug builds. `tests/fixtures/composition_probe.js` qualifies this path
through all three chat endpoints, an Action-only Stop, and web Quit; its explicit
503 replies are not inference/real shutdown proof. For the release-absence gate,
compile `tests/fixtures/probe_import.rs` against the actual libraries: debug must
compile; release must fail **E0432 for `run_development_with_probe`**, even with
`dev-shell` enabled. Rustdoc's parsing cfg does not follow the library's release
cfg reliably, so this gate uses the built library directly (not a source scan):

```text
cargo build --offline --locked -j 2 -p desktop --features dev-shell --lib
cargo build --offline --locked -j 2 -p desktop --features dev-shell --release --lib
rustc --edition=2024 --crate-type=lib --emit=metadata crates/desktop/tests/fixtures/probe_import.rs --extern desktop=$CARGO_TARGET_DIR/debug/libdesktop.rlib -L dependency=$CARGO_TARGET_DIR/debug/deps -o $CARGO_TARGET_DIR/debug/probe_import.rmeta
rustc --edition=2024 --crate-type=lib --emit=metadata crates/desktop/tests/fixtures/probe_import.rs --extern desktop=$CARGO_TARGET_DIR/release/libdesktop.rlib -L dependency=$CARGO_TARGET_DIR/release/deps -o $CARGO_TARGET_DIR/release/probe_import.rmeta
```

Apply the same host guard/nice policy before each compiler invocation. The final
command's expected refusal is a negative acceptance control, not a green exit.

Package hooks (trusted manifest, private source/staging directories only):

```text
desktop-package verify MANIFEST SOURCE
desktop-package stage MANIFEST SOURCE DESTINATION
desktop-package sign-plan MANIFEST
```

The staging command refuses an existing destination, copies only allowlisted
files and verifies the result; failure removes only its exclusively created
staging directory. **It is not an atomic installer/update transaction.** The
sign-plan command emits JSON argument arrays and executes nothing.

## Native layout and release handoff

```text
Mycelium.app/Contents/
  Info.plist
  MacOS/mycelium
  Frameworks/libmlxc.dylib
  Frameworks/libmlx.dylib
  Frameworks/<reviewed transitive dylibs>
  Frameworks/mlx.metallib        (beside libmlx.dylib: MLX looks only there; a copy under Resources/ is not found)
  Resources/licenses/<dependency notices>
```

L8 must build compatible native libraries and provide exact identities/ABI/Metal
compiler and minimum-OS evidence. L12 must resolve every Mach-O install name and
LC_RPATH against this layout (system frameworks or bundle-relative only), reject
workspace/Homebrew rpaths, audit LC_BUILD_VERSION of **every** nested image,
remove build-tree metallib fallback, and prove moved-bundle GPU execution.
The preliminary `relocatable_link` predicate is not that complete Mach-O audit.

Sequence: stage → patch audited install names/rpaths if needed → ad-hoc sign
nested dylibs → executable → outer app (no `--deep` signing shortcut) → verify
all code signatures and resource seal → produce final payload manifest/ZIP and
same-byte qualification. Signing mutates Mach-O bytes, so final checksums must be
recorded **after** signing. The manifest comes from trusted release metadata;
do not trust an unverified manifest beside the executable or embed an executable's
own hash back into that same executable. Metadata/package hooks are not an
independent trust root. Developer ID/notarization remain outside v0.2.

Install/upgrade/rollback, preserved application state, real consent/model/runtime,
L3 parity, status under real compile load, queue/quit races, startup-failure
cleanup, logout/signals, minimum-OS GPU, Finder/Gatekeeper and final signing/ZIP
qualification remain open. No old test is retired by this crate.

## S12/S13 composition adapter

`services::adapt(Arc<S>, NonZeroUsize)` returns an `Arc<ServiceCore<S>>` and
`ServiceCleanup<S>` for the **same** host implementing `Services`. Supply the pair
to `run_packaged`'s existing factory. The capacity is injected from the admitted
host/L1 policy; there is no guessed production default (dedicated L1 ID pending).
It bounds callbacks through delivery, not just queue insertion. Full capacity
returns `503 native_service_capacity`, closed admission returns
`503 application_stopping`, and dropped callbacks prevent Verified cleanup.
No new worker, detached task or executor is created.

| Desktop seam | SLICE-1/service composition responsibility |
|---|---|
| `ServiceRequest { method, target, headers, body }` | Same owned fields as `api::http::Request`; strip only the native URI authority. Preserve values/duplicates and body bytes; do **not** construct `api::desktop::Desktop/Session` or synthesize Host/Origin/cookies. |
| `Services::submit(request, completion)` | Nonblocking bounded enqueue to the host executor. Dispatch strict JSON/actions/status through `api::desktop::Service`, `api::actions::Control<S>` and `PublishedStatus`, not its browser-session wrapper. Preserve errors and publish-before-success. |
| `Services::begin_shutdown(Instant)` | Synchronously fence the host's admission/effects with its submit gate, then enqueue real cancellation and cleanup off the GUI thread. Do not call blocking `NodeService::stop` or `runtime::shutdown::shutdown` directly from this callback. |
| `Services::poll_shutdown()` | Try-read the composed cleanup receipt. Return Verified only after gateway drain, workers/descendants, reservation/resource proof and host callbacks are finished; map real shutdown failures to errors. |
| `Cleanup::bind_clock(Instant)` | Shell calls this with its **actual** monotonic origin before setup. The adapter translates the absolute Span deadline using that origin, not construction time or a fresh full allowance. Bind once; missing/rebound clocks fail closed. |

The native smoke selects this adapter with the explicit harness-only
`--probe --service-capacity 1` arguments; its underlying host remains an inert
unavailable service, not the real worker runtime. An external contract fixture
against the SLICE-1 API pin exercises the real `Control` prepare/execute/publish
path and exact `400 unknown_action` refusal. This is API compatibility evidence,
**not** an integrated NodeService/Metal/worker-cleanup claim. The additive trait
itself has no API dependency; composition tests use existing workspace dev edges.

The adapter's local revoke check does not replace the host's atomic effect fence;
a dispatch already racing shutdown must be refused/cancelled under that fence.
Completion observed after revocation is replaced with `application_stopping`.
Verified additionally waits for all completion callbacks to return. SSE streaming,
full action coverage, real queue/quit authority, node-service shutdown ownership,
and whole-app startup-failure cleanup remain integration gates.

### Real SLICE-1 types, controlled cleanup environment

`--features service-composition` enables tests against the committed
`node_service::desktop_host::DesktopServices` and `NodeService`, through the same
L11 `adapt` pair. The five tests cover real chat dispatch, idle drain and closed
admission, a held callback, active cancellation with missing cleanup proof,
propagated driver failure, and successful typed signed-session settlement. The
last fixture uses real L5/L9/L10 receipt verification with a controlled signed
peer and an explicitly inert residue callback. It is **not** physical worker,
Metal, relay, reservation or native-GUI service cleanup evidence. No sibling
crate is copied or patched to make these tests pass.

The current SLICE-1 `desktop-shell` feature also enables `metal`, even though
these controlled tests never perform inference. Use the admitted local MLX
headers/libraries (`MLXC_INCLUDE_DIR`, `MLXC_LIB_DIR`), with no downloads. For
Apple Silicon macOS tests, an explicit Cargo runner avoids SIP stripping DYLD
variables before the test executable starts:

```text
CARGO_TARGET_AARCH64_APPLE_DARWIN_RUNNER="env DYLD_LIBRARY_PATH=$MLXC_LIB_DIR" cargo test --offline --locked -j 2 -p desktop --features service-composition
```

The compiler/test runner remains inside the owner guard/nice policy. These are
development-only test edges to existing locked packages, not release rpaths or
environment-based production loader overrides.

### System-browser qualification boundary

The explicit read-only OS gate resolves both HTTP and HTTPS with the real
`NSWorkspace URLForApplicationToOpenURL:` method:

```text
cargo test --offline --locked -j 2 -p desktop --features dev-shell --lib system_browser_handler_resolves_without_launch -- --ignored --nocapture
```

It creates no browser tab/window and performs no HTTP request. The real opener
also has non-browser-destination refusal coverage. Positive `openURL:` dispatch
is **NOT_RUN**: it would create a visible browser tab/window on the owner's
screen. Keep the already-tested injected OS-opener seam until an authorized
isolated desktop is available; handler resolution is not proof of browser launch.

## Budget seams

Production cleanup takes an injected typed L1/L6 Span; no numeric duration default
is invented. `RegistryPoll` supplies cleanup-receipt wake cadence. `RouterClose`
is used only for the fake-core development allowance. Native harness readiness
uses `MemberRestartGrace`, AppleEvent send uses `RouterClose`, exit observation
uses `CancelAndCleanup`, and polling uses `OwnerPoll`. These are explicit existing
registry-backed **harness allowances**, not latency measurements or proof of a
production desktop close bound. Request dedicated desktop/harness IDs from L1
before full integration; preserve F16's missing loaded-profile p99 blocker.

## Local unsigned bundle (round 4; not a release)

`desktop-package bundle` assembles `Mycelium.app` into a caller-owned output
directory, exclusively (refuses an existing app, removes a failed one). It never installs,
signs, notarizes, registers or launches anything, and its tests refuse any bundle path outside a
`.hermes/cache/scratch/` directory.

- **Identity (owner ruling 25):** v0.2 has its own name `Mycelium` and bundle identifier
  `org.mycelium.app`, so it sits beside an installed v0.1.4 (`Mycelium A13 Demo`,
  `org.mycelium.a13-macos-demo`) and never reads, writes or deletes its files (ruling 20).
  `icon-windowed.icns` (byte-identical to the certified v0.1.4 icon), `LSMinimumSystemVersion
  15.6.1` and the Info.plist key set are v0.1.4's. Differences, named in a test: name, bundle
  identifier, executable `mycelium` and version `0.2.0`. Window title
  `Mycelium`. The packaged Tauri identity is `tauri.packaged.conf.json`, merged via `TAURI_CONFIG`
  at build time; `tauri.conf.json` stays the development identity.
- **Native payload:** `libjaccl/libmlx/libmlxc.dylib` and `mlx.metallib` are copied from the
  genuine `evidence/foundation-exit/native-inputs.json` after size+SHA-256 checks against that
  manifest; `package-pins.json` (generated by `desktop-package pins`, compared in a test with the
  manifest and the independent L8 `backend-mlx/src/pins.rs` table) is what the executable embeds and admits at
  startup. Seven license notices are copied from the same manifest.
- **Executable (round 7):** the shipped `mycelium` is `node-service`'s bin of that name
  (`cargo build --release -p node-service --features packaged --bin mycelium`), not this crate's
  `src/bin/mycelium.rs` (a core-less shell kept as the test double for entry/launch-wiring tests).
  It routes like v0.1.4, admits the OS and the pinned payload, then serves the unchanged UI over
  SLICE-1's node service (`node_service::packaged`): the pinned Qwen2.5-0.5B is looked up in the
  state root's v0.1.4 `model-source-*` layout, where the window's `/api/model/download` puts it
  (until it is prepared the chat answers 503 `model_not_installed`; then the native service is
  composed over it without a relaunch), private state and per-launch evidence live under
  `<state>/v02-node/`. The same executable is the
  node's member/owner-leaf worker (re-executed with `member|owner-leaf`), admitted first. It links
  `libmlxc` through `@executable_path/../Frameworks` only. Probe hooks do not exist in it: the
  test-only `qualification-probe` feature (scratch bundles for `bundle_chat`) is the only way to
  build them, and `tests/probe_absence.rs` scans the built default bundle for them. Consent is
  SLICE-1's operator fixture and the qualification identity is the development one; there is no
  pairing UI. Its own entry in the admission manifest is self-measured (layout only; the real
  seal is the v0.3 signature), and the dylibs are loaded by dyld before the admission runs.
- **Relay client (cloudflared), lane RELF:** v0.1.4 bundles `cloudflared` beside its code and its
  licence in `third_party/` (`scripts/build_a13_macos.py:285-288,318,325`: whatever `cloudflared`
  was on the builder's PATH, unpinned). v0.2 pins ONE binary in `helper-pins.json`: the exact bytes
  v0.1.4's release ships (cloudflared 2026.10.2 arm64, 25972048 B, sha256 `41423e7c…342c7f`; the
  Homebrew 2026.10.2 bottle re-signed ad-hoc: with both signatures removed it equals Homebrew's
  `eeb8f9bd…` binary except the 3-byte `__LINKEDIT` vmsize field). It ships at
  `Contents/MacOS/native/cloudflared` (where `api::relay::installed_relay_binary` looks) with its
  existing ad-hoc signature kept, and its Apache-2.0 licence at
  `Contents/Resources/licenses/cloudflared-LICENSE` (`58d1e17f…`). `repro/build-bundle.sh` needs
  `MYCELIUM_RELAY_CLIENT` / `MYCELIUM_RELAY_LICENSE` pointing at local copies (on m4pro: the
  unpacked v0.1.4 release under `mycelium-m4-program/incident/deepcheck-v014-20261107T180822Z/unpacked/`
  and `/opt/homebrew/Cellar/cloudflared/2026.10.2/LICENSE`); nothing is downloaded and other bytes
  are refused. Its `LC_BUILD_VERSION` minos is **26.0**, above `LSMinimumSystemVersion` 15.6.1, as in
  v0.1.4: `CONTENT-MANIFEST.json` lists it as a minimum-OS finding (the relay is the owner's; a
  15.6.1 owner cannot start it).
- **Admission is closed over the code directories:** startup admission (and `desktop-package admit`)
  refuses any file or link in `Contents/MacOS` or `Contents/Frameworks` that the manifest does not
  pin (`PackageError::Unpinned`), so a planted `native/cloudflared` or an extra dylib is refused
  before anything can execute it. Dylibs are still loaded by dyld before admission (F8).
- **Outputs next to the app:** `CONTENT-MANIFEST.json` (entries, sizes, sources, trust level,
  minimum-OS findings) and `APP-FILES.sha256` (`shasum -c` compatible).
- **No Python:** `bundle::audit_no_python` (names, shebangs, marker bytes across read boundaries,
  Mach-O load commands via the explicit gate). Unit controls prove each indicator kind and a
  v0.1.4-shaped tree are rejected.

**Reproducible build (round 9).** `crates/desktop/repro/build-bundle.sh OUT_DIR [probe]` is the canonical
recipe (env in `repro/env.sh`; Python-free). tauri bakes the absolute `CARGO_MANIFEST_DIR` into the
exe and `--remap-path-prefix` cannot reach a string literal, so the build runs through one fixed
symlink (`repro/canonical.sh`, default `/Users/Shared/mycelium-v02-canonical-src`, override
`MYCELIUM_CANONICAL_SRC`; mkdir lock). Before lane RELF the executable was NOT reproducible across
target dirs or builders (finding REL-R1, review F4): its `LC_UUID` (16 bytes, and the ad-hoc
signature over it) depended on the `CARGO_TARGET_DIR` path, and it carried the canonical source path,
then under the builder's `$HOME` (username included). Lane RELF item 5 changed the recipe: the
canonical path is no longer under `$HOME`, `CARGO_TARGET_DIR` and `CARGO_HOME` are reached through
fixed links beside it (`$MYCELIUM_CANON.target`, `.cargo-home`), `RUSTFLAGS` is the same text for
every builder, and the linker writes no debug map (`-Wl,-S`; `LC_UUID` is kept, no `-no_uuid`).
Whether two builders now get byte-identical bundles is decided only by running
`repro/verify-two-paths.sh SCRATCH` (two builders: different tree, target dir and `HOME`, all of
different lengths) on the commit in question; this README makes no claim beyond that script's
output, which the release ledger's `two-path-reproducibility` cell records. The ignored test
`two_path_builds_of_the_same_commit_are_byte_identical` runs it; the tiny-crate proof + control and
the same-environment-for-two-builders check are in `tests/repro_canonical.rs`.

```text
source $SCRATCH/env.sh   # (pre-round-9 manual recipe) CARGO_TARGET_DIR, MACOSX_DEPLOYMENT_TARGET=15.6, RUSTFLAGS remap, strip,
                          # TAURI_CONFIG="$(cat crates/desktop/tauri.packaged.conf.json)"
cargo build --release --offline --locked -j 2 -p desktop --features tauri-shell --bin mycelium --bin desktop-package
desktop-package bundle target/release/mycelium evidence/foundation-exit/native-inputs.json crates/desktop/icons/icon-windowed.icns OUT_DIR
MYCELIUM_BUNDLE_APP="OUT_DIR/Mycelium.app" cargo test --release --offline --locked -j 2 -p desktop --test bundle_gate -- --ignored --test-threads=1
MYCELIUM_BUNDLE_APP="OUT_DIR/Mycelium.app" cargo test --release --offline --locked -j 2 -p desktop --features tauri-shell --test bundle_smoke -- --ignored --nocapture --test-threads=1
```

`bundle_smoke` executes only the scratch bundle's own executable (no `open`, no name or bundle-id
addressing), compares the frontmost app before/at-window/settled/after, and quits with an Apple
Event addressed to the exact child pid.

## Install, upgrade, rollback, user state, `Install Mycelium.command` (rounds 5-6)

The per-user installer is ported from v0.1.4 (`installer_core.py` / `mycelium_a13_macos/installer.py`
at f4e366e9) and behaves as v0.1.4 does. `desktop-package` takes explicit roots (the real `$HOME`,
`/Applications` and `~/Applications` are refused: `install_root_is_real_home` /
`install_root_is_real_applications`); the app's own `--install` family (`desktop::entry`) uses `$HOME`
exactly like v0.1.4, so every script and test runs it with `HOME` pointing at a scratch folder.

```text
desktop-package finalize BUNDLE_DIR VERSION ROLLBACK_FLOOR SOURCE_REVISION SIGNING_KEY_FILE
desktop-package install|rollback|uninstall|status HOME APPLICATIONS [BUNDLE_DIR | --delete-identity-and-state]
desktop-package bundle EXECUTABLE NATIVE_INPUTS ICON OUT_DIR [NATIVE_INPUTS_SHA256]
mycelium (--install [--bundle DIR] | --uninstall [--delete-identity-and-state] | --rollback | --install-status)
```

- **Signed package documents (`docs`)**: a bundle carries `installation-package-manifest.json`
  (Ed25519-signed, binds `package_digest`) and `install-source.json` (binds `artifact_digest`; its
  artifact name yields the version). They are verified as v0.1.4 does: closed shapes, the same checks in
  the same order and the same refusal codes, signature domain `mycelium.ed25519_evidence_signature.v1`
  over canonical JSON (Python-repr floats), 90-day expiry, publisher label, publisher-key pin (first use
  pins the offered key; every later install and rollback must match), rollback floor, admitted macOS
  versions exactly 26.5.1 / 15.6.1. Proven on the real v0.1.2 release (its signature verifies, key
  `582670de...`), on a Python-computed signature vector, and on tampered / wrong-key / bad-signature
  fixtures refused with the install root, Applications folder and state byte-identical afterwards.
  `finalize` writes the documents with a key FILE (32 raw bytes, never printed); tests use throw-away
  seeds, the production signer is never read.
- **v0.1.4's layout under v0.2's own folder** (ruling 25) under `HOME`: programs
  `Library/Application Support/org.mycelium.app/programs/<version>` (+ `versions.json`,
  `.install.lock`), link `APPLICATIONS/Mycelium.app`, state `Library/Application
  Support/org.mycelium.app/state` (v0.1.4: `Mycelium/programs`, `Mycelium A13.app`, `Mycelium A13
  macOS Demo`; test `ruling20_v02_install_upgrade_rollback_uninstall_and_launch_routing_beside_v014_touch_no_v014_path`:
  install, upgrade, rollback and both uninstalls are real, the launch is routing only and its first
  writes are simulated; a real model-free launch + Quit beside a seeded v0.1.4 tree is the lane-RELF
  smoke receipt, not a test); the record is v0.1.4's, kept as JSON so unknown
  fields survive; owner-only checks are v0.1.4's (directory exactly 0700, record exactly 0600, one link,
  no link on the path; missing parents are made 0700).
- **Order (v0.1.4's):** verify before any write; lock; `<version>.partial` staging by `/usr/bin/ditto`;
  re-verify the staged copy; only then clear `com.apple.quarantine` on it; rename into place; write the
  record; prune to current + previous; register the Applications link last (a foreign entry is refused
  there, after the install, like v0.1.4). A same-version reinstall drops the rollback target. Uninstall
  reads the record first (a damaged one refuses, changing nothing), unlinks everything under programs
  even the running version (deferred self-removal as v0.1.4: `running_version_removed_after_exit`),
  and keeps state unless `--delete-identity-and-state`.
- **`Install Mycelium.command`** is v0.1.4's script byte for byte except the executable name
  (`mycelium`) and the app name (`Mycelium.app`, ruling 25), pinned by the sha256 of the real v0.1.2 release's script. It runs
  `<app>/Contents/MacOS/mycelium --install --bundle <folder>`.
- **Entry routing (`entry`)**: a bare launch (or `-psn_*`) outside `programs/<version>` prints a
  `launcher_refused` event, shows an alert (`MYCELIUM_LAUNCH_ALERT=off` suppresses it for scripted
  launches) and exits 2 before anything else; the installer flags run the installer; other arguments are
  refused.
- **Known remaining differences from v0.1.4** (all fail closed): ASCII digits only where Python `\d` accepts
  Unicode digits; a removal error during uninstall is reported (`install_io_error`) instead of ignored,
  and a state root that is a link is refused instead of silently skipped; the directory checks are
  path-based, not descriptor-held; `prune_failures` is an added report field; local `finalize` declares
  the launchd agent with a text digest instead of a rendered plist; no zip is written.

### Relocatable rpath for the MLX-linked executable

`build.rs` links the `mycelium` bin with `-Wl,-rpath,@executable_path/../Frameworks` (macOS +
`tauri-shell`). `bundle::build` runs `otool` on the executable and refuses any rpath that is not
bundle-relative (`BundleError::Rpath`, e.g. the absolute rpath `backend-mlx/build.rs` emits), any
dependency outside system/`@rpath`, and an `@rpath` dependency without the Frameworks rpath. Tests
compile tiny dylib/exe probes with clang, check them with `otool`, then move the bundle and run it
with dyld (an absolute-rpath control fails after the move). `bundle_gate` checks the built
executable carries exactly that one rpath.

### Candidate native-inputs manifest

The optional fifth `bundle` argument pins the manifest's sha256; a mismatch refuses before anything
is parsed or assembled. L8's `mycelium.l8.native-inputs-candidate.v1` (the minos-14 rebuild,
`runs/v2/v02/L8-evidence/native-inputs-candidate-minos14.json`) is consumed only with that pin (without
one: `native_inputs_candidate_requires_pin`); every source is still size+sha checked, and its `minos`
feeds the minimum-OS findings. Any other schema is refused even when pinned. A bundle assembled
from the candidate does not pass `bundle_gate`'s identity test nor the shell executable's startup
admission until `package-pins.json` is regenerated from the switched genuine manifest (FOUNDATION-EXIT).

### Quit exit status and the chat envelope (round 10)

- **Quit exit status as v0.1.4's launcher** (`__main__.py:219-223 @ f4e366e9`, executed in
  `SLICE-1-evidence/r8-oracle/launcher-{proven,unproven}.log`): once a Quit has a result the shell
  ends the process with `0` when the cleanup is verified and `3` when it is not, after printing
  `{"event": "launcher_cleanup_failed", "code": "cleanup_incomplete", "state_retained": true}` on
  stderr. It no longer stays up with a retitled window. `Shutdown::exit_status` carries this;
  `can_exit` still means *verified* only (F16). Gates:
  `boundaries::quit_exit_status_is_the_v014_launcher_status` and the real-shell GUI gate
  `tests/quit_exit_codes.rs` (`mycelium-desktop-dev --isolated-dev [--unproven-cleanup]`, exact-pid
  Apple Event, `--ignored`).
- **Chat envelope.** v0.1.4's page posts the user's plain prompt and the gateway codec wraps it in the
  Qwen2 system/user/assistant envelope; the node takes finished ChatML. The packaged chat did not
  wrap, and the page trims the probe's ChatML, so the model began with a whitespace token
  (round 9's `answer_text` was `"\n\n"`). `node_service::chat_template` now wraps at the chat edge
  (seam note `L11-evidence/round10/seam-node-service.md`). `bundle_chat` asserts the oracle-equal
  answer (`tests/support/chat_answer.rs`, fixture `tests/fixtures/chat-answer-oracle.json`,
  FX-L11-chat-answer): the recorded tokens and text must equal v0.1.4's greedy answer for the first
  16 tokens (text-exact prefix when fewer are recorded); a whitespace-only answer is refused. Fast
  tests: `tests/chat_answer_oracle.rs`.

### Quit allowance, menu / Command-Q, LaunchServices launch and reopen (round 12)

- **RU-032 / SE-SLICE1-L11-QUIT-JOIN-60: Quit waits the 60 s L1 id and exits 0 like v0.1.4.**
  v0.1.4's `close()` is `run.stop(); run.join(60)` (`demo_app.py:202-203 @ f4e366e9`) and ignores
  the join's result; executed `SLICE-1-evidence/r10-oracle/` q4: teardown held 75 s -> exit 0 at
  60.01 s, stderr empty, run still "stopping". Round 10's reading (an unproven cleanup exits 3) was
  wrong for a teardown that merely outlasts the join. Now `lifecycle::quit_allowance()` (the L1 id
  `PyMyceliumA13MacosDemoAppCloseRunJoinL203`, 60 s) is the Quit budget of every production entry
  (desktop `mycelium`, node-service packaged `mycelium` and the desktop-shell host, the dev shell;
  they used `RouterClose` 4 s / `StageCleanup` 30 s). `Failed(Deadline)` is exit 0 with no
  `launcher_cleanup_failed` line; `Shutdown::can_exit` still means *verified* only and the shell
  still prints `L11_CLEANUP:Failed(Deadline)`. Exit 3 + the event stay where v0.1.4 has them (a
  close step raised: begin/receipt-poll error; `--unproven-cleanup` fixture). A clock fault or an
  invalid budget keep the stricter 3 (V03-L11-1, inputs v0.1.4 never produces).
  Gates: `boundaries::{quit_exit_status_is_the_v014_launcher_status,
  a_close_step_that_raises_still_exits_3, every_production_shell_entry_uses_the_one_quit_allowance}`
  and the GUI gate
  `quit_exit_codes::quit_with_a_teardown_that_outlasts_the_join_exits_0_after_the_60s_allowance`
  (`--isolated-dev --slow-cleanup`). The packaged executable is pinned by the source-text test only
  (no bundle was rebuilt or launched this round).
- **Menu Quit and Command-Q** (CL-L11-04): `quit_exit_codes::{menu_quit_*, command_q_*,
  menu_then_command_q_share_one_cleanup}`. A Command-Q posted from outside with `CGEventPostToPid`
  reached nothing here (it needs Accessibility and an active app), so the dev shell (`dev-shell` +
  `debug_assertions` only, `native_probe.rs`) lets the trusted probe script ask AppKit to perform
  the real menu item (`performActionForItemAtIndex:`) and a genuine Command-Q key-down `NSEvent`
  through the main menu's `performKeyEquivalent:`.
- **LaunchServices launch and reopen** (CL-L11-03, development build only): `tests/finder_launch.rs`
  starts the dev build with `/usr/bin/open -g`, re-opens it with `open` (the hidden window comes back
  in the same process, `L11_REOPEN:` receipt, one instance) and quits it by exact-pid Apple Event.
  Finder itself, Gatekeeper/quarantine and the packaged bundle are not exercised.
