#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# build-bundle.sh OUT_DIR [probe]  -- canonical packaged build of the tree this script lives in.
# Needs CARGO_TARGET_DIR (explicit). OUT_DIR must not exist. `probe` builds the test-only
# qualification-probe executable instead of the default one.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=env.sh
. "$here/env.sh"
# shellcheck source=canonical.sh
. "$here/canonical.sh"
out=${1:?OUT_DIR}; variant=${2:-default}
[ ! -e "$out" ] || { echo "OUT_DIR exists: $out" >&2; exit 1; }
case "$variant" in
  default) feat=packaged ;;
  probe) feat=qualification-probe ;;
  *) echo "variant: default|probe" >&2; exit 1 ;;
esac
canon_acquire "$ROOT"; trap canon_release EXIT
cd "$ROOT"
m="$MYCELIUM_CANON/Cargo.toml"
nice -n 19 cargo build --release --offline --locked -j 2 --manifest-path "$m" -p node-service --features "$feat" --bin mycelium
nice -n 19 cargo build --release --offline --locked -j 2 --manifest-path "$m" -p desktop --bin desktop-package
exe=$(mktemp "${TMPDIR:-/tmp}/mycelium-exe.XXXXXX")
cp "$CARGO_TARGET_DIR/release/mycelium" "$exe"
mkdir -p "$out"
nice -n 19 "$CARGO_TARGET_DIR/release/desktop-package" bundle "$exe" \
  evidence/foundation-exit/native-inputs.json crates/desktop/icons/icon-windowed.icns "$out"
rm -f "$exe"
echo BUNDLE_DONE "$out"
