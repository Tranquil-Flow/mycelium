#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Print the short head a readiness/PACKAGE receipt is ABOUT, or nothing.

Preserved receipts used to be named after the head that happened to be checked out when the preserving
run started.  Measured 2026-09-20: that renamed a c5ee01a receipt to "-superseded-7c9e262", i.e. it
recorded the wrong provenance on the one kind of file whose whole purpose is provenance.  Prefer the
receipt's own record; fall back to the caller's tag only when the file carries no head at all.
"""
import json
import sys

try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
if not isinstance(doc, dict):
    raise SystemExit(0)
head = doc.get("git_head") or ""
if not head:
    for check in (doc.get("checks") or []):
        head = (check.get("detail") or {}).get("git_head") or ""
        if head:
            break
print(head[:7])
