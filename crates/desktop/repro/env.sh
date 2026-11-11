# SPDX-License-Identifier: AGPL-3.0-only
# Canonical environment of the reproducible packaged build (source it). The caller chooses
# CARGO_TARGET_DIR (it does not reach the output: build-bundle.sh builds through the fixed link
# `$MYCELIUM_CANON_TARGET`) and the tree is the one this file lives in.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)
: "${CARGO_TARGET_DIR:?set CARGO_TARGET_DIR explicitly}"
# shellcheck source=canonical.sh
. "$(dirname "${BASH_SOURCE[0]}")/canonical.sh"
export CARGO_TARGET_DIR
export CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0
export MACOSX_DEPLOYMENT_TARGET=15.6
export MLXC_INCLUDE_DIR=${MLXC_INCLUDE_DIR:-$HOME/Projects/mycelium-spike-mlxc/vendor/mlx-c-57731b00e8bed710e0750e7356da9f18f8eb3e20}
export MLXC_LIB_DIR=${MLXC_LIB_DIR:-$HOME/Projects/mycelium-native-macos14/install/lib}
export TAURI_CONFIG
TAURI_CONFIG=$(cat "$ROOT/crates/desktop/tauri.packaged.conf.json")
# The flags are the same text for every builder (cargo feeds RUSTFLAGS into the output): registry
# paths in panic locations become /build/.cargo/... through the fixed cargo-home link, and the linker
# writes no debug map (`-Wl,-S`: no OSO stabs naming object files under the target dir or the
# toolchain), so LC_UUID no longer depends on where objects were built. LC_UUID itself is kept
# (crash reports and symbolication key on it; `-no_uuid` is not used); the exe is stripped anyway.
export RUSTFLAGS="--remap-path-prefix=$MYCELIUM_CANON_CARGO_HOME=/build/.cargo -C link-arg=-Wl,-S"
export CARGO_PROFILE_RELEASE_STRIP=symbols CARGO_PROFILE_RELEASE_DEBUG=0
