"""Stable fail-closed error code container for the production runner."""
from __future__ import annotations


class RunnerError(ValueError):
    """Fail-closed runner error carrying a deterministic error code.

    Codes are stable strings; the message may include detail but consumers must
    branch on ``code`` rather than text. New codes require a parent-plan review.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("RunnerError requires a non-empty code")
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")
