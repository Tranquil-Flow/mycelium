# SPDX-License-Identifier: AGPL-3.0-only
"""Windows managed host agent CLI (the target side of an A13 managed route).

The macOS owner app pairs, approves and compiles; this host answers it::

  python -m mycelium_a13_windows.managed_host --root C:\\mycelium\\host status
  ... pair-request --policy policy.json --out request.json   # give request.json to the owner
  ... accept-handoff handoff.json                          # the owner's approval
  ... report-capability
  ... descriptor --run-id R --deployment-id D --manifest-digest sha256:... --acknowledge --out card.json
  ... preview --policy policy.json --limits limits.json --out preview.json
  ... run --policy policy.json --limits limits.json --preview preview.json --acknowledge

``run`` accepts exactly the previewed work (``consent_digest``), starts the
native worker, and keeps serving until the owner requests cleanup (which it
performs and acknowledges) or Ctrl+C (which drains and keeps staging).

Consent is explicit on every step; nothing here can broaden a resource policy
or accept a different offer than the one previewed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading

from mycelium_a13_macos.app import consent_valid
from mycelium_a13_macos.worker import WorkerLimits

from .app import WindowsTargetApplication, windows_installation_descriptor

CONSENT = {"cooperative_peer_visibility_acknowledged": True,
           "resource_use_acknowledged": True, "contribution_enabled": False}


def _read(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit("expected a JSON object: " + path)
    return value


def _write(path: str, value) -> None:
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def _policy(path: str) -> dict:
    policy = _read(path)
    if not consent_valid(policy):
        raise SystemExit("resource policy invalid (see mycelium_a13_macos.app.consent_valid)")
    return policy


def _limits(path: str) -> WorkerLimits:
    return WorkerLimits(**_read(path))


def _work_options(app, policy, limits):
    from mycelium_a13_macos.resources import policy_violation

    if limits.max_disk_bytes > policy["storage_bytes"]:
        raise SystemExit("work limits exceed the resource policy")

    def admitted(_intent):
        return policy_violation(policy, app._observe_policy()) is None  # noqa: SLF001

    return {"limits": limits, "resource_policy": policy, "authorize_stage": admitted}


def main(argv=None, *, app_factory=None, descriptor=None, description=None) -> int:
    """The managed host CLI.  The Linux host reuses it with its own app and descriptor."""

    parser = argparse.ArgumentParser(description=description or __doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="this host's private state root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p = sub.add_parser("pair-request")
    p.add_argument("--policy", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("accept-handoff")
    p.add_argument("handoff")
    sub.add_parser("report-capability")
    p = sub.add_parser("descriptor")
    for name in ("--run-id", "--deployment-id", "--manifest-digest", "--out"):
        p.add_argument(name, required=True)
    p.add_argument("--acknowledge", action="store_true")
    for name in ("preview", "run"):
        p = sub.add_parser(name)
        p.add_argument("--policy", required=True)
        p.add_argument("--limits", required=True)
        if name == "preview":
            p.add_argument("--out", required=True)
        else:
            p.add_argument("--preview", required=True)
            p.add_argument("--acknowledge", action="store_true")
            p.add_argument("--keep-staging-on-cleanup-request", action="store_true")
    args = parser.parse_args(argv)

    app = (app_factory or WindowsTargetApplication)(Path(args.root))
    app.start()
    try:
        if args.command == "status":
            print(json.dumps(app.status(), indent=2, sort_keys=True, default=str))
        elif args.command == "pair-request":
            _policy(args.policy)
            _write(args.out, app.peer.pair_request(CONSENT))
        elif args.command == "accept-handoff":
            print(json.dumps(app.accept_pairing(_read(args.handoff)), indent=2, sort_keys=True, default=str))
        elif args.command == "report-capability":
            print(json.dumps(app.report_capability(), indent=2, sort_keys=True, default=str))
        elif args.command == "descriptor":
            _write(args.out, (descriptor or windows_installation_descriptor)(
                app, run_id=args.run_id, deployment_id=args.deployment_id,
                manifest_digest=args.manifest_digest, acknowledged=args.acknowledge))
        elif args.command == "preview":
            from mycelium_a13_macos.work_acceptance import preview_work

            offer = app.pending_stage()
            if offer is None:
                raise SystemExit("no pending offer from the owner yet")
            options = _work_options(app, _policy(args.policy), _limits(args.limits))
            _write(args.out, preview_work(app, offer, stage_acknowledged=True, **options))
        elif args.command == "run":
            from mycelium_a13_macos.work_acceptance import accept_work

            if not args.acknowledge:
                raise SystemExit("--acknowledge is required to accept the previewed work")
            options = _work_options(app, _policy(args.policy), _limits(args.limits))
            accept_work(app, _read(args.preview), work_acknowledged=True, **options)
            offer = app.pending_stage()
            stop = threading.Event()
            # Under the service runner (Task Scheduler member), the runner's named
            # stop event is the graceful stop: it drains exactly like Ctrl+C.
            import mycelium_host_platform

            watcher = mycelium_host_platform.start_stop_event_watcher(stop.set)
            print("serving; Ctrl+C or the supervisor's stop event drains and keeps staging", flush=True)
            try:
                while not stop.wait(0.5):
                    if not args.keep_staging_on_cleanup_request:
                        requested = app.pending_cleanup()
                        if requested is not None:
                            app.stop_control()
                            ack = app.cleanup_offer(requested, acknowledged=True)
                            print(json.dumps({"state": "cleaned", "cleanup_ack": ack}, sort_keys=True))
                            return 0
                    state = app.status()["native_control"]
                    if state.get("state") != "running":
                        print(json.dumps({"state": "stopped", "native_control": state}, default=str))
                        return 1
            except KeyboardInterrupt:
                stop.set()
            finally:
                if watcher is not None:
                    watcher.set()
                app.stop_control()
            if stop.is_set():
                print("draining (staging kept)", flush=True)
            del offer
        return 0
    finally:
        app.stop()


if __name__ == "__main__":
    sys.exit(main())
