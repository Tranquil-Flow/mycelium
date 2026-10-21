#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Capture the APP'S OWN admission status at ONE moment, for attribution across a physical intervention.

WHY THIS EXISTS (round p78r1, ERRATA-23 and the 2026-09-20 re-read).  The round withheld 103 terminals and
nobody could say why, because:

  * the route's incident list lived in memory and died with the app;
  * every status snapshot the chain takes PREDATES the soak;
  * and, worst, ONE OF THE INTERVENTIONS WAS NEVER WRITTEN DOWN.  The recorded file names the owner-side
    worker kill at 19:46:36Z but says nothing about the laptop side, so "did the intervention cause the
    wedge?" cannot be answered from the artifacts at all -- and the wedge began within seconds of the first
    block while the owner-side kill came ~110 s later.  An unrecorded intervention is an unmeasurable
    variable.

So every intervention in a round is bracketed by this script: one capture before, one after, each named by
its stage.  It records

  * the coordinator's own status document (queue depth, active dispatch set against
    maximum_active_requests, per-request phase / terminal_state / queue_wait_ms / reservation_count, and the
    incident list -- last 256), read from whichever channel answers first, with the failures of the other
    channel kept rather than discarded;
  * how many `live route terminal blocked` lines the app log holds SO FAR, which is the live withheld-
    terminal counter the soak's summary only reports at the end;
  * the wall clock, so the captures can be ordered against the intervention log and the managed-control
    ledger by time alone.

Usage:  round_status_capture.py --label p79r1 --stage before-owner-restart [--reason free text]

Never raises on a failed read: it writes what it got and says which channel failed, because a capture that
silently writes nothing is the silent-instrument trap.  Exit: 0 captured · 3 no channel answered.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVIDENCE = Path(os.environ.get(
    "A13_EVIDENCE_DIR",
    os.path.expanduser("~/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac")))
STATUS_PATH = "/__mycelium/runtime/admission-status"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_url(label: str, failures: dict) -> str:
    """The live route's origin: ask the app, else read it out of the round's own supervisor log."""

    try:
        rpc = _load("round14_rpc", "round14_rpc.py")
        status = rpc.owner("/api/serving/status", {}) or {}
        url = ((status.get("serving") or {}).get("startup") or {}).get("url") or ""
        if url:
            return url.rstrip("/")
        failures["app_control_serving_status"] = "no startup url in the response"
    except Exception as exc:
        failures["app_control_serving_status"] = "%s: %s" % (type(exc).__name__, str(exc)[:180])
    log = EVIDENCE / ("round%s-supervisor.log" % label)
    try:
        for match in re.finditer(r"'registry_origin': '([^']+)'", log.read_text()):
            url = match.group(1)
        return url.rstrip("/")
    except Exception as exc:
        failures["supervisor_log"] = "%s: %s" % (type(exc).__name__, str(exc)[:180])
    return ""


def _fetch(url: str, failures: dict) -> object:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        failures["live_http"] = "%s: %s" % (type(exc).__name__, str(exc)[:180])
    try:
        rpc = _load("round14_rpc", "round14_rpc.py")
        return rpc.owner(STATUS_PATH, {})
    except Exception as exc:
        failures["app_control"] = "%s: %s" % (type(exc).__name__, str(exc)[:180])
    return None


def _blocked_so_far(label: str) -> int | None:
    path = EVIDENCE / ("round%s-owner-daemon.log" % label)
    try:
        return len(re.findall(r"live route terminal blocked", path.read_text(errors="replace")))
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True, help="the round's label, e.g. p79r1")
    parser.add_argument("--stage", required=True,
                        help="names this capture, e.g. before-owner-restart / after-network-change")
    parser.add_argument("--reason", default="", help="what is about to happen, or what just happened")
    args = parser.parse_args()

    failures: dict[str, str] = {}
    base = _base_url(args.label, failures)
    document = _fetch(base + STATUS_PATH, failures) if base else None
    payload = {
        "protocol": "mycelium.a13.round_status_capture.v1",
        "label": args.label,
        "stage": args.stage,
        "reason": args.reason,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "registry_origin": base or None,
        "channel": None if document is None else ("live_http" if "live_http" not in failures else "app_control"),
        "blocked_terminal_lines_so_far": _blocked_so_far(args.label),
        "failures": failures,
        "document": document,
    }
    out = EVIDENCE / ("round%s-status-%s.json" % (args.label, args.stage))
    out.write_text(json.dumps(payload, sort_keys=True, indent=1))

    summary = {"stage": args.stage, "channel": payload["channel"],
               "blocked_terminal_lines_so_far": payload["blocked_terminal_lines_so_far"]}
    if isinstance(document, dict):
        queue = document.get("queue") or {}
        requests = document.get("requests") or []
        summary.update({
            "queue_depth": queue.get("depth"),
            "active": "%s/%s" % (len(queue.get("active_request_ids") or []),
                                 queue.get("maximum_active_requests")),
            "recorded_requests": len(requests),
            "incidents": len(document.get("incidents") or []),
        })
    print("STATUS_CAPTURE", json.dumps(summary, sort_keys=True))
    print("STATUS_CAPTURE_FILE", out)
    if document is None:
        print("  -> NO CHANNEL ANSWERED.  That is a finding about the CAPTURE, not about the route: record it")
        print("     and re-read by hand while the app is still up.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
