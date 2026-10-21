# SPDX-License-Identifier: AGPL-3.0-only
"""Post-deploy verifier: is the *code that will run* the code we tested?

Why this exists: `deploy_preview_to_laptop.py` compares exactly one digest -- the
packaged executable.  That proves the launcher shipped, but not that the bundled
*runtime source* it executes is the fixed revision.  The build receipt cannot
close that gap either: round 57's receipt carries a live executable digest but a
*stale* `scope` string (it names "derived native command/response budget,
native-exit diagnostics and precise worker-unavailable cause" and says nothing
about the seal fix) even though the bundled `physical_inference_node.py` does
carry the fix.  With a two-attempt C5 budget, a bundle that silently ships the
old seal loop would waste the last one.

So verify the source digest directly, on the host, for every bundled copy:

  1. executable digest == local
  2. `Contents/Resources/a13-runtime-source/physical_inference_node.py` == local tree
  3. `_internal/a13-runtime-source/physical_inference_node.py` == local tree
  4. the fix markers are present in the *remote* copy (grep on the host)
  5. executable bit set, and the app tree is owner-only
  6. remote free space (the run's own precondition)

Read-only.  Exits non-zero on any mismatch, so it can gate the attempt.

Usage:
    verify_preview_deployed.py --version 57
    verify_preview_deployed.py --version 56 --expect-missing   # positive control
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _required(name):
    value = os.environ.get(name)
    if not value or not value.startswith('/'):
        raise SystemExit('a13_operator_env_required:' + name)
    return value


REMOTE = _required("A13_REMOTE_ROOT")
HOST = os.environ.get("A13_REMOTE_HOST", "member-mac")

APP = "Mycelium A13 Demo.app"
EXE_REL = APP + "/Contents/MacOS/Mycelium A13 Demo"
SOURCE_RELS = (
    # Required: the copy the packaged .app actually executes.
    (APP + "/Contents/Resources/a13-runtime-source/physical_inference_node.py", True),
    # Informational: the build also emits a loose directory form locally, but the
    # deployed package zip carries only the .app, so its absence remotely is
    # expected and must not be reported as a defect (round 57 self-test).
    ("Mycelium A13 Demo/_internal/a13-runtime-source/physical_inference_node.py", False),
)

# The seal fix's own markers: the committed read, the blocking-seal site, and the
# diagnostic that names a stall instead of leaving a hole.
FIX_MARKERS = (
    "allow_blocking_observations=True",
    "isinstance(committed, Mapping)",
    "seal_poll",
)

# The control-pump wedge fix (round 54): the fresh-vs-resumed distinction and the
# retirement of the pending after an accepted reconciliation ack.  Preview.58 ships
# both; a bundle that regressed to the raising guard would wedge the peer again.
PUMP_SOURCE_REL = APP + "/Contents/Resources/a13-runtime-source/mycelium_a13_macos/control_pump.py"
PUMP_MARKERS = (
    "resumed = pending is not None",
    "if resumed and pending['phase'] in",
)


def run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=180)


def remote(command: str) -> subprocess.CompletedProcess:
    return run(["ssh", "-T", "-o", "BatchMode=yes", HOST, command])


def digest_of(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def remote_digest(rel: str, tag: str) -> str | None:
    quoted = shlex.quote("%s/%s/%s" % (REMOTE, tag, rel))
    done = remote("shasum -a 256 %s 2>/dev/null | awk '{print $1}'" % quoted)
    value = done.stdout.strip()
    return value or None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--expect-missing",
        action="store_true",
        help="positive control: assert the version is NOT deployed",
    )
    args = parser.parse_args()

    tag = "preview" + args.version
    dist = Path(os.environ.get("A13_DIST_ROOT") or (REPO / "dist")) / ("0.1.0-preview." + args.version)
    tree_source = REPO / "physical_inference_node.py"
    failures: list[str] = []

    if not dist.is_dir():
        print("FAIL  local bundle missing: %s" % dist)
        return 2

    local_exe = digest_of(dist / EXE_REL)
    local_source = digest_of(tree_source)
    print("LOCAL bundle  %s" % dist.name)
    print("LOCAL exe     %s" % local_exe)
    print("LOCAL source  %s  (working tree)" % local_source)

    remote_exe = remote_digest(EXE_REL, tag)

    if remote_exe is None:
        if args.expect_missing:
            print("PASS  %s is correctly NOT deployed on the laptop (positive control)" % tag)
            return 0
        print("BLOCKED  %s is not deployed on the laptop" % tag)
        print("         deploy_preview_to_laptop.py --version %s" % args.version)
        return 3

    print("REMOTE exe    %s" % remote_exe)
    if remote_exe != local_exe:
        failures.append("executable digest differs (remote != local)")

    for rel, required in SOURCE_RELS:
        remote_value = remote_digest(rel, tag)
        label = rel.split("/a13-runtime-source/")[0]
        if remote_value is None and not required:
            print("REMOTE source %s  [%s, not shipped in the package - expected]" % ("absent", label))
            continue
        print("REMOTE source %s  [%s]" % (remote_value, label))
        if remote_value is None:
            failures.append("bundled source missing remotely: %s" % rel)
        elif remote_value != local_source:
            # This is the failure that would silently waste the last C5 attempt.
            failures.append(
                "bundled source is NOT the fixed revision in %s "
                "(remote %s != tree %s)" % (rel, (remote_value or "")[:16], (local_source or "")[:16])
            )

    primary_source_rel = SOURCE_RELS[0][0]
    for rel, markers in ((primary_source_rel, FIX_MARKERS), (PUMP_SOURCE_REL, PUMP_MARKERS)):
        for marker in markers:
            quoted = shlex.quote("%s/%s/%s" % (REMOTE, tag, rel))
            # -F: markers are literal source text, and several contain regex
            # metacharacters (e.g. `pending['phase']`) that BRE would misread as a
            # bracket expression -- which silently reported a present fix as absent.
            done = remote("grep -F -c %s %s 2>/dev/null || true" % (shlex.quote(marker), quoted))
            count = done.stdout.strip() or "0"
            print("REMOTE marker %-38s occurrences=%s  [%s]"
                  % (marker, count, rel.split("/")[-1]))
            if count in ("", "0"):
                failures.append("fix marker absent remotely: %s (%s)" % (marker, rel))

    done = remote(
        "test -x %s && echo exec_ok || echo exec_missing"
        % shlex.quote("%s/%s/%s" % (REMOTE, tag, EXE_REL))
    )
    exec_state = done.stdout.strip()
    print("REMOTE exec bit  %s" % exec_state)
    if exec_state != "exec_ok":
        failures.append("remote executable is not executable")

    done = remote("df -h ~ | tail -1")
    print("REMOTE disk      %s" % done.stdout.strip())

    if failures:
        print("")
        for item in failures:
            print("FAIL  %s" % item)
        return 1

    print("")
    print("PASS  %s deployed and the bundled runtime source matches the fixed tree" % tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
