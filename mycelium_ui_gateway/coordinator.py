"""Injected swarm coordinator authority seam."""
from __future__ import annotations

from typing import Any, Awaitable, Mapping, Protocol, Union


CoordinatorResult = Union[Mapping[str, Any], Awaitable[Mapping[str, Any]]]


class SwarmCoordinator(Protocol):
    """Device-session authority injected by deployment; never Router authority."""

    def status(self) -> CoordinatorResult: ...

    def create_invite(self, document: Mapping[str, Any]) -> CoordinatorResult: ...

    def join(self, document: Mapping[str, Any]) -> CoordinatorResult: ...

    def leave(self, document: Mapping[str, Any]) -> CoordinatorResult: ...


class CoordinatorError(RuntimeError):
    """Optional stable public failure from a coordinator implementation."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__("coordinator_error")


__all__ = ["CoordinatorError", "SwarmCoordinator"]
