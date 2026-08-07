"""Bounded lifecycle state and public persisted state schema."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import RunnerError

STATE_PROTOCOL = "mycelium.physical_runner_state.v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class RunnerState(str, Enum):
    PREPARING = "preparing"
    LOADING = "loading"
    UNREADY = "unready"
    QUALIFIED = "qualified"
    FAILED = "failed"
    CLEANUP_COMPLETE = "cleanup-complete"


@dataclass(frozen=True, slots=True)
class RunStateDocument:
    plan_id: str
    run_id: str
    operator_plan_path: str
    command: str
    state: RunnerState
    updated_at_unix_ms: int
    route_ready: bool
    manifest_digest: str | None = None
    qualification_id: str | None = None

    def __post_init__(self) -> None:
        if not self.plan_id or not self.run_id:
            raise RunnerError("state_invalid", "identity")
        if self.command not in {"prepare", "diagnose", "qualify", "cancel", "recover", "cleanup"}:
            raise RunnerError("state_invalid", "command")
        if not isinstance(self.updated_at_unix_ms, int) or isinstance(self.updated_at_unix_ms, bool) or self.updated_at_unix_ms < 0:
            raise RunnerError("state_invalid", "updated_at_unix_ms")
        if self.route_ready and self.state is not RunnerState.QUALIFIED:
            raise RunnerError("state_invalid", "route_ready_without_qualification")
        if self.manifest_digest is not None and _SHA256_RE.fullmatch(self.manifest_digest) is None:
            raise RunnerError("state_invalid", "manifest_digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": STATE_PROTOCOL,
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "operator_plan_path": self.operator_plan_path,
            "command": self.command,
            "state": self.state.value,
            "updated_at_unix_ms": self.updated_at_unix_ms,
            "route_ready": self.route_ready,
            "manifest_digest": self.manifest_digest,
            "qualification_id": self.qualification_id,
        }


__all__ = ["RunnerState", "RunStateDocument", "STATE_PROTOCOL"]
