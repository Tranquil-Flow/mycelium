# SPDX-License-Identifier: AGPL-3.0-only
# Fixed build paths for reproducible packaged builds (source this file).
#
# `tauri::generate_context!()` (tauri-macros without `custom-protocol` = "dev" context) bakes the
# ABSOLUTE `CARGO_MANIFEST_DIR` of the `desktop` crate into the executable as a string literal
# (`Context::with_config_parent`; tauri-codegen absolutises a relative value with the cwd, so no
# RUSTC wrapper can hide it). `--remap-path-prefix` does not touch string literals. The path is
# therefore made identical on purpose: every canonical build is run through ONE fixed symlink that
# points at whichever worktree is being built, `cargo build --manifest-path $MYCELIUM_CANON/Cargo.toml`.
# Lane RELF item 5 (review F4, REL-R1): the fixed path no longer depends on the builder's $HOME
# (it used to default to `$HOME/.hermes/...`, so the username shipped in the exe and another account
# got other bytes), and the CARGO_TARGET_DIR and CARGO_HOME are reached through fixed links beside it
# too (`$MYCELIUM_CANON.target`, `$MYCELIUM_CANON.cargo-home`), so no builder path reaches the linker
# or rustc. The default lives in /Users/Shared (present on every Mac, not tied to one account); a
# path there that is not our own link is refused, never replaced.
# A mkdir lock serialises users of the links (a dead owner's lock is broken).
MYCELIUM_CANON=${MYCELIUM_CANONICAL_SRC:-/Users/Shared/mycelium-v02-canonical-src}
MYCELIUM_CANON_TARGET="$MYCELIUM_CANON.target"
MYCELIUM_CANON_CARGO_HOME="$MYCELIUM_CANON.cargo-home"
canon_link() { # canon_link PATH TARGET : point our own link at TARGET; anything else is refused
  local path=$1
  if [ -e "$path" ] || [ -L "$path" ]; then
    [ -L "$path" ] || { echo "canonical path exists and is not a link: $path" >&2; return 1; }
    [ "$(stat -f %u "$path")" = "$(id -u)" ] || { echo "canonical link belongs to another user: $path" >&2; return 1; }
  fi
  ln -sfn "$2" "$path"
}
canon_acquire() { # canon_acquire TREE [TARGET_DIR CARGO_HOME]
  local lock="$MYCELIUM_CANON.lock" owner
  [ ! -e "$MYCELIUM_CANON" ] || [ -L "$MYCELIUM_CANON" ] || { echo "canonical path exists and is not a link" >&2; return 1; }
  until mkdir "$lock" 2>/dev/null; do
    owner=$(cat "$lock/pid" 2>/dev/null || true)
    if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then rm -rf "$lock"; continue; fi
    sleep 5
  done
  echo $$ > "$lock/pid"
  canon_link "$MYCELIUM_CANON" "$1" || { rm -rf "$lock"; return 1; }
  if [ $# -ge 3 ]; then
    canon_link "$MYCELIUM_CANON_TARGET" "$2" && canon_link "$MYCELIUM_CANON_CARGO_HOME" "$3" \
      || { canon_release; return 1; }
  fi
}
canon_release() {
  local link
  for link in "$MYCELIUM_CANON" "$MYCELIUM_CANON_TARGET" "$MYCELIUM_CANON_CARGO_HOME"; do
    [ ! -L "$link" ] || rm -f "$link"
  done
  rm -rf "$MYCELIUM_CANON.lock"
}
