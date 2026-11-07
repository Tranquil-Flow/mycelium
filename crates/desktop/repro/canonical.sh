# SPDX-License-Identifier: AGPL-3.0-only
# Fixed build path for reproducible packaged builds (source this file).
#
# `tauri::generate_context!()` (tauri-macros without `custom-protocol` = "dev" context) bakes the
# ABSOLUTE `CARGO_MANIFEST_DIR` of the `desktop` crate into the executable as a string literal
# (`Context::with_config_parent`; tauri-codegen absolutises a relative value with the cwd, so no
# RUSTC wrapper can hide it). `--remap-path-prefix` does not touch string literals. The path is
# therefore made identical on purpose: every canonical build is run through ONE fixed symlink that
# points at whichever worktree is being built, `cargo build --manifest-path $MYCELIUM_CANON/Cargo.toml`.
# A mkdir lock serialises users of the link (a dead owner's lock is broken).
MYCELIUM_CANON=${MYCELIUM_CANONICAL_SRC:-$HOME/.hermes/cache/scratch/mycelium-canonical-src}
canon_acquire() { # canon_acquire TREE
  local lock="$MYCELIUM_CANON.lock" owner
  [ ! -e "$MYCELIUM_CANON" ] || [ -L "$MYCELIUM_CANON" ] || { echo "canonical path exists and is not a link" >&2; return 1; }
  until mkdir "$lock" 2>/dev/null; do
    owner=$(cat "$lock/pid" 2>/dev/null || true)
    if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then rm -rf "$lock"; continue; fi
    sleep 5
  done
  echo $$ > "$lock/pid"
  ln -sfn "$1" "$MYCELIUM_CANON"
}
canon_release() {
  [ ! -L "$MYCELIUM_CANON" ] || rm -f "$MYCELIUM_CANON"
  rm -rf "$MYCELIUM_CANON.lock"
}
