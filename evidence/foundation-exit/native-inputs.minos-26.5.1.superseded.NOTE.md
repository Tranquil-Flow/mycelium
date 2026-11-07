<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Superseded native-input manifest (minos 26.5.1)

`native-inputs.minos-26.5.1.superseded.json` is the **byte-identical** previous
`native-inputs.json` (sha256 `40a9980d20a18e051eecb67ccfe16c45bfe79f45a6afb1a946d8a30e32542061`,
34,209 bytes, observed 2026-10-27T22:14:11Z). It is kept as evidence only and is **not** read by any
code or env var.

Why superseded (integrator decision, FOUNDATION-EXIT round 5, 2026-10-28): its libraries record
`LC_BUILD_VERSION minos 26.5.1` (`libmlx`, `libjaccl`) and `26.0` (`libmlxc`). dyld refuses such
images on macOS 15.x, where v0.1.4 (libmlx minos 14.0, app `LSMinimumSystemVersion` 15.6.1) runs, so
shipping them would be a user-visible regression. The replacement `native-inputs.json` pins L8's
offline rebuild with `MACOSX_DEPLOYMENT_TARGET=14.0` (`candidate-minos-14.0`).

Its pin set `foundation-exit-minos-26.5.1` (libmlxc `524cfcb7…`, libmlx `b05f5026…`, libjaccl
`cd74c235…`, mlx.metallib `ad012813…`) was removed from `crates/backend-mlx/src/pins.rs`; those
digests remain only as test inputs that `pins::select` must refuse
(`superseded_minos_26_5_1_libraries_are_refused_naming_every_stale_file`). Receipts that cite this
file's hash (`seal.json` history, `FOUNDATION-EXIT-round2-evidence-manifest.json`) describe the
round 2–4 state and stay valid for it; round 5 re-seals the current manifest.
