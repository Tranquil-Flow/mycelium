#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# verify-two-paths.sh SCRATCH_DIR
# Builds the canonical packaged bundle of the committed HEAD twice, SEQUENTIALLY, from two `git archive`
# copies at DIFFERENT paths (different depth and name length) with DIFFERENT, fresh CARGO_TARGET_DIRs,
# and compares the sha256 of every bundle file. Tree A and target A are deleted (after an lsof check)
# as soon as bundle A is hashed, to keep disk use to one tree + one target. Exit 0 only when both
# file lists and all hashes are identical.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
repo=$(git -C "$here" rev-parse --show-toplevel)
scratch=${1:?SCRATCH_DIR}
case "$scratch" in "$HOME/.hermes/cache/scratch/"*) ;; *) echo "SCRATCH_DIR must be under ~/.hermes/cache/scratch" >&2; exit 1 ;; esac
[ -z "$(git -C "$repo" status --porcelain)" ] || { echo "dirty tree: commit first" >&2; exit 1; }
commit=$(git -C "$repo" rev-parse HEAD)
mkdir -p "$scratch"
a="$scratch/repro-a/src"; b="$scratch/repro-bb-longer-name/deeper/tree-b"
tga="$scratch/target-a"; tgb="$scratch/target-b"
[ ! -e "$scratch/out-a" ] && [ ! -e "$scratch/out-b" ] && [ ! -e "$scratch/repro-a" ] || { echo "scratch not clean: rename old results first" >&2; exit 1; }
list() { (cd "$1" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256); }
drop() { # drop DIR : remove a scratch dir of ours once nothing has it open
  if lsof +D "$1" >/dev/null 2>&1; then echo "still open, not removing: $1" >&2; return 1; fi
  rm -rf "$1"
}
echo "commit $commit"
mkdir -p "$a"; git -C "$repo" archive "$commit" | tar -x -C "$a"
echo "A $a target $tga"
CARGO_TARGET_DIR=$tga "$a/crates/desktop/repro/build-bundle.sh" "$scratch/out-a"
list "$scratch/out-a" > "$scratch/out-a.sha256"
drop "$tga"; drop "$scratch/repro-a"; echo "A target and tree removed after hashing"
mkdir -p "$b"; git -C "$repo" archive "$commit" | tar -x -C "$b"
echo "B $b target $tgb"
CARGO_TARGET_DIR=$tgb "$b/crates/desktop/repro/build-bundle.sh" "$scratch/out-b"
list "$scratch/out-b" > "$scratch/out-b.sha256"
drop "$tgb"; drop "$scratch/repro-bb-longer-name"
wc -l < "$scratch/out-a.sha256" | xargs echo "files in A:"
cat "$scratch/out-a.sha256"
if cmp -s "$scratch/out-a.sha256" "$scratch/out-b.sha256"; then echo "REPRODUCIBLE: identical file lists and sha256 (A=$a B=$b)"; rc=0
else echo "DIFFERENT:"; diff "$scratch/out-a.sha256" "$scratch/out-b.sha256" || true; rc=1; fi
exit $rc
