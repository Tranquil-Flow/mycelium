#!/usr/bin/env python3
"""Append one derived A15 release decision generation and write its public projection.

``decide`` derives the decision from the supplied candidate, checkout, evidence roots and
pinned trust registry. ``revoke`` appends an explicit non-ready generation. The decision
time is the system clock; it is not an argument, so stale evidence cannot be made fresh.

Exit codes: 0 a ``ready`` generation was appended; 1 a ``not_ready`` generation was
appended; 2 the producer refused to run and appended nothing; 3 the generation was
appended but the public projection could not be written.

Standard output and standard error carry bounded JSON only: digests, a generation number
and stable codes. Paths and operating-system error text are never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_a15_release_decision import (  # noqa: E402
    REVOCATION_REASONS,
    DecisionResult,
    ReleaseDecisionRefused,
    append_revocation,
    check_exclusive_target,
    produce_release_decision,
    public_projection_bytes,
    write_exclusive,
)
from mycelium_qualification.evidence import sha256_bytes  # noqa: E402


class _ArgumentsInvalid(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _ArgumentsInvalid


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    decide = commands.add_parser("decide")
    decide.add_argument("--candidate", required=True)
    decide.add_argument("--repo-root", required=True)
    decide.add_argument("--evidence-root", action="append", default=[], metavar="ID=DIRECTORY")
    decide.add_argument("--trust-registry", required=True)
    decide.add_argument("--trust-registry-sha256", required=True)
    decide.add_argument("--ledger-dir", required=True)
    decide.add_argument("--public-out", required=True)
    decide.add_argument(
        "--synthetic-self-test",
        action="store_true",
        help="admit a synthetic_test_fixture trust registry; grants no release authority",
    )
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--ledger-dir", required=True)
    revoke.add_argument("--reason", required=True, choices=REVOCATION_REASONS)
    revoke.add_argument("--public-out", required=True)
    return parser


def _evidence_roots(arguments: list[str]) -> dict[str, str]:
    roots: dict[str, str] = {}
    for argument in arguments:
        root_id, separator, directory = argument.partition("=")
        if not separator or not root_id or not directory or root_id in roots:
            raise ReleaseDecisionRefused("evidence_root_argument_invalid")
        roots[root_id] = directory
    return roots


def _emit(stream: object, payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True), file=stream)  # type: ignore[arg-type]


def main(
    argv: Sequence[str] | None = None, *, clock: Callable[[], datetime] | None = None
) -> int:
    try:
        arguments = _parser().parse_args(argv)
    except _ArgumentsInvalid:
        _emit(sys.stderr, {"refused": "arguments_invalid"})
        return 2
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    try:
        if arguments.command == "decide":
            roots = _evidence_roots(arguments.evidence_root)
            protected = (
                arguments.candidate,
                arguments.repo_root,
                arguments.trust_registry,
                arguments.ledger_dir,
                *roots.values(),
            )
            check_exclusive_target(arguments.public_out, protected_paths=protected)
            result: DecisionResult = produce_release_decision(
                candidate_path=arguments.candidate,
                repo_root=arguments.repo_root,
                evidence_roots=roots,
                trust_registry_path=arguments.trust_registry,
                trust_registry_sha256=arguments.trust_registry_sha256,
                ledger_dir=arguments.ledger_dir,
                now=now,
                allow_synthetic_trust=arguments.synthetic_self_test,
            )
        else:
            protected = (arguments.ledger_dir,)
            check_exclusive_target(arguments.public_out, protected_paths=protected)
            result = append_revocation(
                ledger_dir=arguments.ledger_dir, reason=arguments.reason, now=now
            )
    except ReleaseDecisionRefused as refused:
        _emit(sys.stderr, {"refused": refused.code})
        return 2
    except Exception:  # noqa: BLE001 - never print a traceback that could carry paths
        _emit(sys.stderr, {"refused": "internal_error"})
        return 2

    appended = {
        "authority_scope": result.document["body"]["authority_scope"],
        "decision": result.decision,
        "decision_digest": result.decision_digest,
        "decision_generation": result.decision_generation,
    }
    try:
        public = public_projection_bytes(result.sealed_bytes)
        write_exclusive(arguments.public_out, public, protected_paths=protected)
    except ReleaseDecisionRefused as refused:
        _emit(sys.stderr, {**appended, "public_projection_refused": refused.code})
        return 3
    _emit(sys.stdout, {**appended, "public_digest": sha256_bytes(public)})
    return 0 if result.decision == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
