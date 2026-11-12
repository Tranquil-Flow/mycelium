#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# lane RELF item 5, two-builder check at the lane bundle's commit (outside the repo; time-boxed variant of
# crates/desktop/repro/verify-two-paths.sh). Builder A = the lane bundle already built by the canonical
# recipe from the worktree (~/Projects/mycelium-v02-m4-laneF, CARGO_TARGET_DIR <worktree>/target, HOME
# /Users/mycelium-dev). Builder B = a `git archive` of the same commit under a different-length scratch
# HOME "Users/another-builder-name", tree and target dir of different lengths (verify-two-paths.sh's B half,
# same commands). Compares every bundle file's sha256; on a difference prints the differing exe bytes and
# both LC_UUIDs. Usage: verify-b-only.sh COMMIT A_OUT_DIR SCRATCH EVIDENCE_TXT
set -uo pipefail
commit=${1:?}; aout=${2:?}; scratch=${3:?}; evtxt=${4:?}
repo=$HOME/Projects/mycelium-v02-m4-laneF
W=$HOME/Projects/mycelium-m4-program/qualification/laneRELF-work
case "$scratch" in "$HOME/.hermes/cache/scratch/"*) ;; *) echo "scratch must be under ~/.hermes/cache/scratch" >&2; exit 1 ;; esac
[ ! -e "$scratch" ] || { echo "scratch exists" >&2; exit 1; }
home_b="$scratch/Users/another-builder-name"
b="$home_b/Projects/much-deeper/mycelium-tree-b"; tgb="$home_b/build/cargo-target-directory-b"
list() { (cd "$1" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256); }
{
echo "commit $commit"; echo "A: worktree $repo target $repo/target HOME $HOME out $aout"
mkdir -p "$b"; git -C "$repo" archive "$commit" | tar -x -C "$b"
echo "B: tree $b target $tgb HOME $home_b"; date
env HOME="$home_b" CARGO_TARGET_DIR="$tgb" CARGO_HOME="$HOME/.cargo" RUSTUP_HOME="$HOME/.rustup" \
  PATH="$HOME/.cargo/bin:$PATH" TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" \
  MYCELIUM_RELAY_CLIENT=$W/relay-inputs/cloudflared MYCELIUM_RELAY_LICENSE=$W/relay-inputs/cloudflared-LICENSE \
  MLXC_INCLUDE_DIR=$HOME/Projects/mycelium-spike-mlxc/vendor/mlx-c-57731b00e8bed710e0750e7356da9f18f8eb3e20 \
  MLXC_LIB_DIR=$HOME/Projects/mycelium-native-macos14/install/lib \
  "$b/crates/desktop/repro/build-bundle.sh" "$scratch/out-b" > "$scratch/build-b.log" 2>&1
echo "B build exit $?"; date
list "$aout" > "$scratch/out-a.sha256"; list "$scratch/out-b" > "$scratch/out-b.sha256"
echo "files in A: $(wc -l < "$scratch/out-a.sha256" | tr -d ' ') in B: $(wc -l < "$scratch/out-b.sha256" | tr -d ' ')"
cat "$scratch/out-a.sha256"
exe_a="$aout/Mycelium.app/Contents/MacOS/mycelium"; exe_b="$scratch/out-b/Mycelium.app/Contents/MacOS/mycelium"
for f in "$exe_a" "$exe_b"; do echo "$(otool -l "$f" | grep -A2 LC_UUID | grep uuid | tr -s ' ') $f"; done
for f in "$exe_a" "$exe_b"; do echo "builder-path strings in $f: $(strings -a "$f" | grep -cE "$HOME|another-builder-name|cargo-target-directory|mycelium-v02-m4-laneF")"; done
if cmp -s "$scratch/out-a.sha256" "$scratch/out-b.sha256"; then echo "REPRODUCIBLE: identical file lists and sha256 (A=worktree builder, B=$b)"
else
  echo "DIFFERENT:"; diff "$scratch/out-a.sha256" "$scratch/out-b.sha256"
  echo "exe differing bytes: $(cmp -l "$exe_a" "$exe_b" | wc -l | tr -d ' ') (first 40 offsets, octal values)"
  cmp -l "$exe_a" "$exe_b" | head -40
fi
} 2>&1 | sed "s#$HOME/.hermes/cache/scratch#<scratch>#g" | tee "$evtxt"
