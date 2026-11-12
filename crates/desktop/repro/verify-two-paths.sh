#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# verify-two-paths.sh SCRATCH_DIR
# Builds the canonical packaged bundle of the committed HEAD twice, SEQUENTIALLY, from two `git archive`
# copies as two different builders would: tree, CARGO_TARGET_DIR and HOME all differ, and so do their
# path LENGTHS (lane RELF item 5: the old equal-length `target-a`/`target-b` hid REL-R1, the exe's
# LC_UUID depending on the target path). Builder B's HOME is a scratch `Users/<another name>` folder
# (CARGO_HOME, RUSTUP_HOME and the pinned native inputs stay the real ones: nothing is downloaded).
# Compares the sha256 of every bundle file; on a difference prints the differing exe bytes and both
# LC_UUIDs. Tree A and target A are deleted (after an lsof check) as soon as bundle A is hashed, to
# keep disk use to one tree + one target. Exit 0 only when both file lists and all hashes are identical.
# Needs MYCELIUM_RELAY_CLIENT / MYCELIUM_RELAY_LICENSE (see build-bundle.sh).
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
repo=$(git -C "$here" rev-parse --show-toplevel)
scratch=${1:?SCRATCH_DIR}
case "$scratch" in "$HOME/.hermes/cache/scratch/"*) ;; *) echo "SCRATCH_DIR must be under ~/.hermes/cache/scratch" >&2; exit 1 ;; esac
[ -z "$(git -C "$repo" status --porcelain)" ] || { echo "dirty tree: commit first" >&2; exit 1; }
: "${MYCELIUM_RELAY_CLIENT:?}" "${MYCELIUM_RELAY_LICENSE:?}"
commit=$(git -C "$repo" rev-parse HEAD)
mkdir -p "$scratch"
a="$scratch/a/src"; tga="$scratch/t-a"
home_b="$scratch/Users/another-builder-name"
b="$home_b/Projects/much-deeper/mycelium-tree-b"; tgb="$home_b/build/cargo-target-directory-b"
[ ! -e "$scratch/out-a" ] && [ ! -e "$scratch/out-b" ] && [ ! -e "$scratch/a" ] && [ ! -e "$scratch/Users" ] || { echo "scratch not clean: rename old results first" >&2; exit 1; }
list() { (cd "$1" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256); }
drop() { # drop DIR : remove a scratch dir of ours once nothing has it open
  if lsof +D "$1" >/dev/null 2>&1; then echo "still open, not removing: $1" >&2; return 1; fi
  rm -rf "$1"
}
real_cargo_home=${CARGO_HOME:-$HOME/.cargo}; real_rustup_home=${RUSTUP_HOME:-$HOME/.rustup}
mlxc_include=${MLXC_INCLUDE_DIR:-$HOME/Projects/mycelium-spike-mlxc/vendor/mlx-c-57731b00e8bed710e0750e7356da9f18f8eb3e20}
mlxc_lib=${MLXC_LIB_DIR:-$HOME/Projects/mycelium-native-macos14/install/lib}
echo "commit $commit"
mkdir -p "$a"; git -C "$repo" archive "$commit" | tar -x -C "$a"
echo "A tree $a target $tga HOME $HOME"
CARGO_TARGET_DIR=$tga CARGO_HOME=$real_cargo_home RUSTUP_HOME=$real_rustup_home \
  MLXC_INCLUDE_DIR=$mlxc_include MLXC_LIB_DIR=$mlxc_lib \
  "$a/crates/desktop/repro/build-bundle.sh" "$scratch/out-a"
list "$scratch/out-a" > "$scratch/out-a.sha256"
cp "$scratch/out-a/Mycelium.app/Contents/MacOS/mycelium" "$scratch/exe-a"
drop "$tga"; drop "$scratch/a"; echo "A target and tree removed after hashing"
mkdir -p "$b"; git -C "$repo" archive "$commit" | tar -x -C "$b"
echo "B tree $b target $tgb HOME $home_b"
env HOME="$home_b" CARGO_TARGET_DIR="$tgb" CARGO_HOME="$real_cargo_home" RUSTUP_HOME="$real_rustup_home" \
  MLXC_INCLUDE_DIR="$mlxc_include" MLXC_LIB_DIR="$mlxc_lib" \
  "$b/crates/desktop/repro/build-bundle.sh" "$scratch/out-b"
list "$scratch/out-b" > "$scratch/out-b.sha256"
drop "$tgb"; drop "$scratch/Users"
wc -l < "$scratch/out-a.sha256" | xargs echo "files in A:"
cat "$scratch/out-a.sha256"
if cmp -s "$scratch/out-a.sha256" "$scratch/out-b.sha256"; then echo "REPRODUCIBLE: identical file lists and sha256 (A=$a B=$b)"; rc=0
else
  echo "DIFFERENT:"; diff "$scratch/out-a.sha256" "$scratch/out-b.sha256" || true
  exe_b="$scratch/out-b/Mycelium.app/Contents/MacOS/mycelium"
  echo "exe differing bytes: $(cmp -l "$scratch/exe-a" "$exe_b" | wc -l | tr -d ' ') (first 20 offsets below)"
  cmp -l "$scratch/exe-a" "$exe_b" | head -20 || true
  for f in "$scratch/exe-a" "$exe_b"; do echo "$(otool -l "$f" | grep -A2 LC_UUID | grep uuid | tr -s ' ') $f"; done
  rc=1
fi
exit $rc
