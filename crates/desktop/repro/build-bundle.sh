#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# build-bundle.sh OUT_DIR [probe]  -- canonical packaged build of the tree this script lives in.
# Needs CARGO_TARGET_DIR (explicit). OUT_DIR must not exist. `probe` builds the test-only
# qualification-probe executable instead of the default one.
# Needs MYCELIUM_RELAY_CLIENT and MYCELIUM_RELAY_LICENSE: local files holding the pinned relay client
# (`helper-pins.json`: cloudflared 2026.10.2 arm64, sha256 41423e7c..., the exact binary v0.1.4
# bundles) and its licence (sha256 58d1e17f..., `package::RELAY_LICENSE_SHA256`). Nothing is
# downloaded: `desktop-package bundle` refuses any other bytes. Where to take them from on the build
# Mac: README "Relay client (cloudflared)".
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=env.sh
. "$here/env.sh"
out=${1:?OUT_DIR}; variant=${2:-default}
relay=${MYCELIUM_RELAY_CLIENT:?set MYCELIUM_RELAY_CLIENT to the pinned cloudflared file}
relay_license=${MYCELIUM_RELAY_LICENSE:?set MYCELIUM_RELAY_LICENSE to the pinned cloudflared licence file}
# The native iroh transport sits beside the relay client in the v0.1.4 bundle; runs spawn it to
# reach other Macs, so it is packaged and pinned like any other helper.
iroh_sidecar=${MYCELIUM_IROH_SIDECAR:-$(dirname "$relay")/mycelium-iroh-sidecar}
[ ! -e "$out" ] || { echo "OUT_DIR exists: $out" >&2; exit 1; }
case "$variant" in
  default) feat=packaged ;;
  probe) feat=qualification-probe ;;
  *) echo "variant: default|probe" >&2; exit 1 ;;
esac
real_target=$CARGO_TARGET_DIR; real_cargo_home=${CARGO_HOME:-$HOME/.cargo}
mkdir -p "$real_target"
canon_acquire "$ROOT" "$real_target" "$real_cargo_home"; trap canon_release EXIT
# Every builder-specific path is reached through the fixed links (canonical.sh): the source tree,
# the target dir and the cargo home.
export CARGO_TARGET_DIR=$MYCELIUM_CANON_TARGET CARGO_HOME=$MYCELIUM_CANON_CARGO_HOME
cd "$ROOT"
m="$MYCELIUM_CANON/Cargo.toml"
nice -n 19 cargo build --release --offline --locked -j 2 --manifest-path "$m" -p node-service --features "$feat" --bin mycelium --bin mycelium-hello-bootstrap
nice -n 19 cargo build --release --offline --locked -j 2 --manifest-path "$m" -p desktop --bin desktop-package --bin resource_probe
exe=$(mktemp "${TMPDIR:-/tmp}/mycelium-exe.XXXXXX")
cp "$CARGO_TARGET_DIR/release/mycelium" "$exe"
mkdir -p "$out"
# The pinned resource probe goes beside the executable; the bundle is then sealed ad-hoc
# (v0.1.4 trust level, SIGN-RECEIPT.json) and its manifests describe the signed bytes.
nice -n 19 "$CARGO_TARGET_DIR/release/desktop-package" bundle "$exe" \
  evidence/foundation-exit/native-inputs.json crates/desktop/icons/icon-windowed.icns "$out" \
  --resource-probe "$CARGO_TARGET_DIR/release/resource_probe" \
  --relay-client "$relay" --relay-license "$relay_license" \
  --iroh-sidecar "$iroh_sidecar" \
  --hello-bootstrap "$CARGO_TARGET_DIR/release/mycelium-hello-bootstrap" --sign-adhoc
rm -f "$exe"
echo BUNDLE_DONE "$out"
