# SPDX-License-Identifier: AGPL-3.0-only
"""The physical-round ledger, and the only way a round may be consumed.

The ledger is the recorded file the round-bringup lane already keeps
(``ATTEMPT-LEDGER.json``: ``<hypothesis>.cap`` / ``.spent`` / ``.starts``), with
the same conservative semantics the one-shot manager used: a round is charged at
serve launch, not on success.  The watchdog adds two hard refusals on top:

* a spend past ``cap`` is refused (``round_budget_exhausted``);
* a spend without a ``source_commit`` is refused (``round_record_incomplete``).

Historical hypotheses are never rewritten.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

__all__ = ["RoundLedger", "RoundLedgerError"]

CONSUMED_STATE = "conservatively_consumed_at_serve_launch"


class RoundLedgerError(RuntimeError):
    """A refused ledger operation; the message names the reason code."""


class RoundLedger:
    """Read/modify/write access to one hypothesis section of the ledger."""

    def __init__(self, path: str | os.PathLike[str], *, hypothesis: str = "h4") -> None:
        self.path = Path(path)
        self.hypothesis = hypothesis

    # ---------------------------------------------------------------- reading

    def read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text())

    def _section(self, document: Mapping[str, Any]) -> Mapping[str, Any]:
        section = document.get(self.hypothesis)
        if not isinstance(section, Mapping):
            raise RoundLedgerError(f"round_ledger_section_missing:{self.hypothesis}")
        return section

    def spent(self) -> int:
        return int(self._section(self.read()).get("spent", 0))

    def cap(self) -> int:
        return int(self._section(self.read()).get("cap", 0))

    def starts(self) -> list[Any]:
        section = self._section(self.read())
        recorded = section.get("starts")
        return list(recorded) if isinstance(recorded, list) else []

    def headroom(self) -> int:
        return self.cap() - self.spent()

    def has_headroom(self) -> bool:
        return self.headroom() > 0

    # ---------------------------------------------------------------- writing

    def spend(
        self,
        receipt: Mapping[str, Any],
        *,
        state: str = CONSUMED_STATE,
    ) -> int:
        """Charge exactly one round.  Returns 1, or raises :class:`RoundLedgerError`."""

        document = self.read()
        section = document.setdefault(self.hypothesis, {"cap": 0, "spent": 0, "starts": []})
        spent = int(section.get("spent", 0))
        cap = int(section.get("cap", 0))
        if spent >= cap:
            raise RoundLedgerError("round_budget_exhausted")
        if not isinstance(receipt, Mapping) or not receipt.get("source_commit"):
            raise RoundLedgerError("round_record_incomplete")

        record = {
            "at": float(receipt.get("at", time.time())),
            "source_commit": str(receipt["source_commit"]),
            "receipt": receipt.get("receipt"),
            "state": state,
        }
        starts = section.get("starts")
        section["starts"] = list(starts) if isinstance(starts, list) else []
        section["starts"].append(record)
        section["spent"] = spent + 1
        self._write(document)
        return 1

    def _write(self, document: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
        with temporary.open("w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
