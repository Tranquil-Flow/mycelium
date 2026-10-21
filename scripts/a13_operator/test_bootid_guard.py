#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fixture test for bootid_guard v2.

Two halves, because both can fail:
  A) SYNTHETIC positive controls -- prove the guard CAN say FIRED and CAN say defect. Without these, a guard
     that always says DID_NOT_FIRE would look perfect on real data (this is the ERRATA-6 lesson: a test that
     cannot fail proves nothing).
  B) REAL logs -- the three known fixtures must come out exactly as measured (ERRATA-9 §2).

Run: env -u PYTHONPATH /opt/homebrew/bin/python3 -B scripts/a13_operator/test_bootid_guard.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootid_guard as g  # noqa: E402

RUNS = "/Users/owner/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac"

# An app-log header so the input-hygiene check is satisfied.
APP_HEAD = '{"event": "launcher_started", "distribution": "unsigned_development_demo"}\n'

SYNTHETIC = [
    ("success-line", APP_HEAD + "boot_id_observations_collected nodes=2\n", "FIRED", 0),
    ("success-line-1-node", APP_HEAD + "boot_id_observations_collected nodes=1\n", "FIRED", 0),
    ("zero-nodes-is-not-success", APP_HEAD + "boot_id_observations_collected nodes=0\n",
     "no_attempt", 6),
    ("refresh-nonempty", APP_HEAD + 'boot_id_refreshed {"remote-mac-a": "boot-1"}\n', "FIRED", 0),
    ("refresh-empty-is-not-success", APP_HEAD + "boot_id_refreshed {}\n", "no_attempt", 6),
    ("scope-error", APP_HEAD + "boot_id_observation_unavailable UnboundLocalError(\"cannot access local "
     "variable 'collect_managed_hello_observations'\")\n", "defect", 6),
    ("name-error", APP_HEAD + "boot_id_observation_unavailable NameError('x')\n", "defect", 6),
    ("producer-refusal", APP_HEAD + "boot_id_observation_unavailable "
     "ManagedObservationsError('managed_control_response_timeout')\n", "refused_expected", 6),
    ("silence", APP_HEAD + "nothing to see\n", "no_attempt", 6),
    ("scope-error-beats-refusal", APP_HEAD + "boot_id_observation_unavailable "
     "ManagedObservationsError('x')\nUnboundLocalError: boom\n", "defect", 6),
]

REAL = [
    (f"{RUNS}/roundsoak04-owner-daemon.log", "defect", 6),
    (f"{RUNS}/roundp74r1-owner-daemon.log", "refused_expected", 6),
    (f"{RUNS}/roundp74r1-attempt.log", "no_attempt", 6),
]

fails = 0


def check(label, path, want_cls, want_code):
    global fails
    v = g.decide(path)
    got = "FIRED" if v.kind == "FIRED" else v.cls
    ok = (got == want_cls and v.code == want_code)
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<32} want={want_cls}/{want_code} got={got}/{v.code}")
    if not ok:
        fails += 1


print("A) SYNTHETIC CONTROLS (the guard must be able to say FIRED and to say defect)")
with tempfile.TemporaryDirectory() as td:
    for name, content, want_cls, want_code in SYNTHETIC:
        p = os.path.join(td, name + ".log")
        with open(p, "w") as fh:
            fh.write(content)
        check(name, p, want_cls, want_code)

print()
print("B) REAL LOGS (measured fixtures from ERRATA-9 §2)")
for path, want_cls, want_code in REAL:
    check(os.path.basename(path), path, want_cls, want_code)

print()
print("C) INPUT HYGIENE (a harness log must be flagged, not silently trusted)")
v = g.decide(f"{RUNS}/roundp74r1-attempt.log")
flag = not v.app_log
print(f"  {'PASS' if flag else 'FAIL'}  p74r1-attempt.log flagged input=not_an_app_log: {not v.app_log}")
if not flag:
    fails += 1
v2 = g.decide(f"{RUNS}/roundp74r1-owner-daemon.log")
ok2 = v2.app_log
print(f"  {'PASS' if ok2 else 'FAIL'}  p74r1-owner-daemon.log accepted as an app log: {v2.app_log}")
if not ok2:
    fails += 1

print()
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
