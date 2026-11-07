#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Regenerates xtask/fixtures/PROVENANCE.tsv rows for xtask's own fixtures (run from xtask/).
# Lint fixtures: kind spec (expected findings = the `// expect:` annotations, authored from the
# README rule table). Gate logs: kind recorded (copied from registry/logs/*; counts re-derived
# by probes/gate-counts.awk and cross-checked against tools/v02.py parse, see the L12 report).
set -eu
cd "$(dirname "$0")/.."
out=fixtures/PROVENANCE.tsv
sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
awk_sha=$(sha probes/gate-counts.awk)
{
  printf 'path\tfixture_id\tkind\tprobe\tprobe_sha256\toracle_sha\tcase_count\tfixture_sha256\n'
  for kind in accept reject; do
    for f in fixtures/$kind/*.rs; do
      name=$(basename "$f" .rs)
      if [ "$kind" = reject ]; then n=$(grep -o '// expect: .*' "$f" | sed 's#// expect: ##' | wc -w | tr -d ' '); else n=0; fi
      printf '%s\tFX-L4-lint-%s-%s\tspec\txtask/README.md rule table (SE001-SE005 TM001 TM002); expected findings are the // expect: annotations\t-\t-\t%s\t%s\n' "$f" "$kind" "$name" "$n" "$(sha "$f")"
    done
  done
  for rule in crypto guards; do
    case $rule in
      crypto) cite='FIPS 180-4 / FIPS 202 / FIPS 197 / RFC 2104 5869 7748 8439 published constants (cited in each file); expected findings are the // expect: annotations' ;;
      guards) cite='xtask/README.md CG001 definition (constant guards in test code); expected findings are the // expect: annotations' ;;
    esac
    for kind in accept reject; do
      for f in fixtures/$rule/$kind/*.rs; do
        name=$(basename "$f" .rs)
        if [ "$kind" = reject ]; then n=$(grep -o '// expect: .*' "$f" | sed 's#// expect: ##' | wc -w | tr -d ' '); else n=0; fi
        printf '%s\tFX-L4-%s-%s-%s\tspec\t%s\t-\t-\t%s\t%s\n' "$f" "$rule" "$kind" "$name" "$cite" "$n" "$(sha "$f")"
      done
    done
  done
  for f in fixtures/gate-logs/*.log; do
    name=$(basename "$f" .log)
    n=$(grep -c '^test result: ' "$f" || true)
    printf '%s\tFX-L12-gate-%s\trecorded\tprobes/gate-counts.awk + tools/v02.py parse over a copy of runs/v2/v02/registry/logs/*\t%s\t-\t%s\t%s\n' "$f" "$name" "$awk_sha" "$n" "$(sha "$f")"
  done
  # Release state machine fixtures (round 11): authored from the release.rs rules; the digests are
  # re-derived in the tests with /usr/bin/shasum. Authoring tool: L12-evidence/r11/gen-release-fixtures.py.
  while IFS= read -r f; do
    rel=${f#fixtures/release/}
    id=$(printf '%s' "$rel" | tr '/.' '--')
    case $f in
      *.jsonl) n=$(grep -c . "$f") ;;
      *.log) n=$(grep -c '^test result: ' "$f" || true) ;;
      *) n=1 ;;
    esac
    printf '%s\tFX-L12-release-%s\tspec\tL12-evidence/r11/gen-release-fixtures.py (authored from xtask/src/release.rs rules and the xtask gate record shape); expected verdicts are asserted in tests/release_machine.rs\t-\t-\t%s\t%s\n' "$f" "$id" "$n" "$(sha "$f")"
  done < <(find fixtures/release -type f | LC_ALL=C sort)
  # R6 retirement fixtures: authored.
  for f in fixtures/retire/*.json; do
    name=$(basename "$f" .json)
    printf '%s\tFX-L12-retire-%s\tspec\tR6 test-retirement design (xtask/retirement/README.md): authored inventory, obligations and an approved registry\t-\t-\t1\t%s\n' "$f" "$name" "$(sha "$f")"
  done
} > "$out"
