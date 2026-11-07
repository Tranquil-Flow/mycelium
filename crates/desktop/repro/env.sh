# SPDX-License-Identifier: AGPL-3.0-only
# Canonical environment of the reproducible packaged build (source it). The caller chooses
# CARGO_TARGET_DIR (it does not reach the output) and the tree is the one this file lives in.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)
: "${CARGO_TARGET_DIR:?set CARGO_TARGET_DIR explicitly}"
export CARGO_TARGET_DIR
export CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0
export MACOSX_DEPLOYMENT_TARGET=15.6
export MLXC_INCLUDE_DIR=${MLXC_INCLUDE_DIR:-$HOME/Projects/mycelium-spike-mlxc/vendor/mlx-c-57731b00e8bed710e0750e7356da9f18f8eb3e20}
export MLXC_LIB_DIR=${MLXC_LIB_DIR:-$HOME/Projects/mycelium-native-macos14/install/lib}
export TAURI_CONFIG
TAURI_CONFIG=$(cat "$ROOT/crates/desktop/tauri.packaged.conf.json")
# Registry and std paths in panic locations become /build/.cargo/... whatever $HOME is.
export RUSTFLAGS="--remap-path-prefix=$HOME=/build"
export CARGO_PROFILE_RELEASE_STRIP=symbols CARGO_PROFILE_RELEASE_DEBUG=0
