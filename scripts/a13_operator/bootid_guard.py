#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""bootid_guard v2 -- did the app-side boot-id collection actually fire in this round?

METHOD (v1's method, kept -- see ERRATA-9): decide from the APP'S OWN VOCABULARY, in the APP'S OWN LOG.
This guard deliberately reads NOTHING but the log it is given. It must never consult
`chain-runs/<run>/observations.json` or `compiled/operator-plan.json`: those are written by the CHAIN's own
managed-hello path, which has succeeded in all 23 runs on disk -- including soak04's, whose app-side
collection raised UnboundLocalError and collected nothing. They cannot discriminate. (ERRATA-8 cited them as
proof the fix worked; ERRATA-9 retracts that.)

THE APP'S VOCABULARY (mycelium_a13_macos/workflow.py):
  :263  boot_id_observations_collected nodes=N      <- the SUCCESS line; never yet emitted by any round
  :265  boot_id_observation_unavailable <exc repr>  <- a REFUSAL, and the reason decides the class
  :279  boot_id_refreshed {json}                    <- peers' boot_id values actually moved

VERDICTS
  exit 0  FIRED           collection returned >= 1 node, or a non-empty refresh was reported
  exit 6  DID_NOT_FIRE    with class=defect | refused_expected | no_attempt
  exit 7  NO_LOG          no log at all

CLASSES
  defect            NameError / UnboundLocalError in the collection path -- the ERRATA-6 defect class.
                    A programming error must never look like a missing hello.
  refused_expected  ManagedObservationsError(...) -- the producer's only refusal type; the binding is left
                    without observations (its inert default) and the round proceeds as before the change.
  no_attempt        no boot-id line at all: either a bundle predating the feature, or the collection never ran.

USAGE
  bootid_guard.py --app-log <path>            decide one log
  bootid_guard.py --corpus <dir>              decide every round*.log and ASSERT the three known fixtures
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

FIRED = 0
DID_NOT_FIRE = 6
NO_LOG = 7

# An app log carries the app's OWN lifecycle lines. The registry protocol string is NOT a signature: the round
# harness quotes it inside its own RESULT line, which is how a harness log passed as an app log in the fixture
# test (caught by test_bootid_guard.py half C). These three are app-only.
APP_SIGNATURES = (
    "launcher_started",
    "packaged_app_closed",
    "live route terminal blocked",
)

RE_COLLECTED = re.compile(r"boot_id_observations_collected nodes=(\d+)")
RE_UNAVAILABLE = re.compile(r"boot_id_observation_unavailable (.+)")
RE_REFRESHED = re.compile(r"boot_id_refreshed (\{.*\})")
RE_SCOPE_ERROR = re.compile(r"UnboundLocalError|NameError")

# Fixtures that MUST hold, measured on the real logs (ERRATA-9 §2). (log, expected class or "FIRED")
EXPECTED = {
    "roundsoak04-owner-daemon.log": "defect",
    "roundp74r1-owner-daemon.log": "refused_expected",
    "roundp74r1-attempt.log": "no_attempt",
}


class Verdict:
    def __init__(self, code: int, kind: str, cls: str, reason: str, app_log: bool):
        self.code, self.kind, self.cls, self.reason, self.app_log = code, kind, cls, reason, app_log

    def line(self, name: str, verbose: bool = True) -> str:
        head = f"{self.kind}"
        if self.cls:
            head += f" class={self.cls}"
        out = f"{name:<38} {head}  (exit {self.code})"
        if not self.app_log and verbose:
            out += "\n    input=not_an_app_log: no app signature found; a round-harness log carries no " \
                   "app vocabulary, so an absence here means nothing"
        if self.reason and verbose:
            out += f"\n    reason: {self.reason}"
        return out


def decide(path: str) -> Verdict:
    if not os.path.isfile(path):
        return Verdict(NO_LOG, "NO_LOG", "", f"no such log: {path}", False)
    text = open(path, errors="replace").read()
    app_log = any(s in text for s in APP_SIGNATURES)

    collected = [int(m) for m in RE_COLLECTED.findall(text)]
    unavailable = RE_UNAVAILABLE.findall(text)
    refreshed = RE_REFRESHED.findall(text)

    # FIRED needs the app's OWN success evidence.
    if any(n >= 1 for n in collected):
        return Verdict(FIRED, "FIRED", "", f"boot_id_observations_collected nodes={max(collected)}",
                       app_log)
    # boot_id_refreshed with a non-empty mapping means peer values really moved, which requires a collection.
    for r in refreshed:
        body = r.strip()
        if body not in ("{}", "{ }"):
            return Verdict(FIRED, "FIRED", "", f"boot_id_refreshed {body[:120]}", app_log)

    # A scope error outranks everything: it is a defect pretending to be an expected condition.
    scope = RE_SCOPE_ERROR.search(text)
    if scope is not None:
        return Verdict(DID_NOT_FIRE, "DID_NOT_FIRE", "defect",
                       f"{scope.group(0)} in the collection path (the ERRATA-6 defect class)", app_log)
    if unavailable:
        return Verdict(DID_NOT_FIRE, "DID_NOT_FIRE", "refused_expected",
                       f"the producer refused: {unavailable[0][:160]}", app_log)
    return Verdict(DID_NOT_FIRE, "DID_NOT_FIRE", "no_attempt",
                   "no boot-id line at all (bundle predates the feature, or the collection never ran)",
                   app_log)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-log")
    ap.add_argument("--corpus")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.app_log:
        v = decide(a.app_log)
        print(v.line(os.path.basename(a.app_log)))
        return v.code

    if not a.corpus:
        ap.error("one of --app-log or --corpus is required")

    logs = sorted(glob.glob(os.path.join(a.corpus, "round*.log")))
    print(f"bootid_guard v2 -- corpus {a.corpus} ({len(logs)} logs)")
    print(f"{'log':<38} verdict")
    print("-" * 96)
    tally, failures = {}, []
    for p in logs:
        name = os.path.basename(p)
        v = decide(p)
        tally[(v.kind, v.cls or "")] = tally.get((v.kind, v.cls or ""), 0) + 1
        print(v.line(name, verbose=False))
        if name in EXPECTED:
            want = EXPECTED[name]
            got = "FIRED" if v.kind == "FIRED" else v.cls
            ok = got == want
            print(f"      FIXTURE {'PASS' if ok else 'FAIL'}  expected={want} got={got}")
            if not ok:
                failures.append((name, want, got))
    print("-" * 96)
    for (kind, cls), n in sorted(tally.items()):
        print(f"  {kind}{(' class=' + cls) if cls else '':<22} {n}")
    print()
    if failures:
        print(f"FIXTURE FAILURES: {len(failures)}")
        for name, want, got in failures:
            print(f"   {name}: expected {want}, got {got}")
        return 1
    print("all known fixtures PASS "
          "(soak04 -> defect; p74r1 app log -> refused_expected; p74r1 harness log -> no_attempt)")
    print()
    print("NOTE: 'no_attempt' on the pre-feature rounds is expected and is NOT a regression. The only round")
    print("      whose APP attempted the collection is p74r1, and it refused (managed_control_response_timeout).")
    print("      The success line has never been observed. Do not claim the fix fired without it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
