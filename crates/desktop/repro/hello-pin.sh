#!/bin/bash
# Print the sha256/bytes of the hello child built exactly as `build-bundle.sh` builds it:
# same canonical source tree, same target dir, same flags. The pin in helper-pins.json must be the
# bytes this recipe produces, the way the resource_probe pin is.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=env.sh
. "$here/env.sh"
real_target=$CARGO_TARGET_DIR
real_cargo_home=${CARGO_HOME:-$HOME/.cargo}
mkdir -p "$real_target"
canon_acquire "$ROOT" "$real_target" "$real_cargo_home"
trap canon_release EXIT
export CARGO_TARGET_DIR=$MYCELIUM_CANON_TARGET CARGO_HOME=$MYCELIUM_CANON_CARGO_HOME
cd "$ROOT"
m="$MYCELIUM_CANON/Cargo.toml"
nice -n 19 cargo build --release --offline --locked -j 2 --manifest-path "$m" \
  -p node-service --features packaged --bin mycelium-hello-bootstrap
f="$CARGO_TARGET_DIR/release/mycelium-hello-bootstrap"
echo "PATH=$f"
echo "BYTES=$(stat -f %z "$f")"
echo "SHA256=$(shasum -a 256 "$f" | cut -d' ' -f1)"
