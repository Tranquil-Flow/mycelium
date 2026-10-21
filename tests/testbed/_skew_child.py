# SPDX-License-Identifier: AGPL-3.0-only
"""Child process that records one skewed-clock exchange into a shared run dir.

Invoked by ``test_trace_clock_skew.py``.  The child applies a *deliberate* clock
offset to both its monotonic and its wall readings, which is how one machine
emulates two hosts whose epochs differ.  It writes only through the public
``mycelium_testbed.trace`` surface, so the test exercises the same code path a
physical run would use.

Line protocol (stdout is line-buffered and flushed explicitly):

``responder``: ``ready`` -> (reads ``ping <hex>``) -> ``reply <hex>``
``initiator``: ``ping <hex>`` -> (reads ``reply <hex>``) -> ``done``
"""

from __future__ import annotations

import argparse
import hashlib
import sys

from mycelium_testbed.trace import (
    EventKind,
    HostClock,
    TraceRunDirectory,
    TraceRecorder,
)


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _payloads(request_id: str, command_id: str) -> tuple[bytes, bytes]:
    ping = f"testbed-ping-{request_id}".encode("utf-8")
    reply = f"testbed-reply-{request_id}-{command_id}".encode("utf-8")
    return ping, reply


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skew-child")
    parser.add_argument("--role", choices=("initiator", "responder"), required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--placement-id", default="node-c-stage-002")
    parser.add_argument("--monotonic-offset-ns", type=int, default=0)
    parser.add_argument("--wall-offset-ns", type=int, default=0)
    return parser


def run_initiator(args: argparse.Namespace) -> int:
    run = TraceRunDirectory.attach(args.run_dir)
    recorder = TraceRecorder(
        args.host_id,
        run=run,
        clock=HostClock(
            monotonic_offset_ns=args.monotonic_offset_ns,
            wall_offset_ns=args.wall_offset_ns,
        ),
        role="initiator",
    )
    ping, _reply = _payloads(args.request_id, args.command_id)
    recorder.event(
        EventKind.ENQUEUE,
        request_id=args.request_id,
        detail={"frame_digest": digest(ping), "hop": "initiator->responder"},
    )
    sys.stdout.write(f"ping {ping.hex()}\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line.startswith("reply "):
        raise SystemExit(f"initiator_unexpected_line:{line.strip()!r}")
    reply = bytes.fromhex(line.split(" ", 1)[1].strip())
    recorder.event(
        EventKind.DELIVERED,
        request_id=args.request_id,
        detail={"frame_digest": digest(reply), "hop": "responder->initiator"},
    )
    run.close()
    sys.stdout.write("done\n")
    sys.stdout.flush()
    return 0


def run_responder(args: argparse.Namespace) -> int:
    run = TraceRunDirectory.attach(args.run_dir)
    recorder = TraceRecorder(
        args.host_id,
        run=run,
        clock=HostClock(
            monotonic_offset_ns=args.monotonic_offset_ns,
            wall_offset_ns=args.wall_offset_ns,
        ),
        role="responder",
    )
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line.startswith("ping "):
        raise SystemExit(f"responder_unexpected_line:{line.strip()!r}")
    ping = bytes.fromhex(line.split(" ", 1)[1].strip())
    recorder.event(
        EventKind.DELIVERED,
        request_id=args.request_id,
        detail={"frame_digest": digest(ping), "hop": "initiator->responder"},
    )
    recorder.event(
        EventKind.STARTED,
        request_id=args.request_id,
        command_id=args.command_id,
        placement_id=args.placement_id,
        detail={"phase": "DECODE", "token_index": 0},
    )
    recorder.event(
        EventKind.FINISHED,
        request_id=args.request_id,
        command_id=args.command_id,
        placement_id=args.placement_id,
        status="ok",
        detail={"phase": "DECODE", "token_index": 0},
    )
    _ping, reply = _payloads(args.request_id, args.command_id)
    recorder.event(
        EventKind.ENQUEUE,
        request_id=args.request_id,
        detail={"frame_digest": digest(reply), "hop": "responder->initiator"},
    )
    sys.stdout.write(f"reply {reply.hex()}\n")
    sys.stdout.flush()
    run.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.role == "initiator":
        return run_initiator(args)
    return run_responder(args)


if __name__ == "__main__":
    raise SystemExit(main())
