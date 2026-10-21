# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

"""A cold restart after peer loss must tolerate an old sidecar that is exiting.

The retired route's ``close()`` returns while the old owner of the node's
sidecar socket is still draining (a remote worker behind a dropped control
link, or a sidecar mid-exit). The replacement reopens the same socket root;
that must wait a bounded time for the exiting owner and still refuse a live
foreign owner that outlasts the bound.
"""

from pathlib import Path
import secrets
import shutil
import socket
import threading
import time

import pytest

from physical_inference_node import NativeSidecarProcess
from tests.live.test_cold_route_restart import _Route
from mycelium_live.supervisor import (
    COLD_ROUTE_RESTART_FAILED_CODE,
    ColdRouteRestartError,
    ColdRouteRestartOwner,
)


class _SocketOwnerRoute(_Route):
    """A route whose node owns an accepting sidecar socket that outlives close()."""

    def __init__(self, name: str, socket_root: Path, *, linger_seconds: float) -> None:
        super().__init__(name)
        self.socket_root = socket_root
        self.linger_seconds = linger_seconds
        self.listener: socket.socket | None = None
        self.release = threading.Event()

    def hold_socket(self) -> None:
        self.socket_root.mkdir(parents=True, exist_ok=True)
        path = self.socket_root / "i.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(8)
        self.listener = listener

    def close(self) -> None:
        super().close()
        # close() returns at once; the old owner exits (and unlinks) later.
        threading.Thread(target=self._exit_later, daemon=True).start()

    def _exit_later(self) -> None:
        self.release.wait(self.linger_seconds)
        listener, self.listener = self.listener, None
        if listener is not None:
            listener.close()
            try:
                (self.socket_root / "i.sock").unlink()
            except OSError:
                pass
            try:
                self.socket_root.rmdir()
            except OSError:
                pass


class _ReopeningRoute(_Route):
    """A replacement whose open prepares the same sidecar socket root."""

    def __init__(self, name: str, socket_root: Path, grace_seconds: float | None) -> None:
        super().__init__(name)
        self.socket_root = socket_root
        self.grace_seconds = grace_seconds

    def open(self):
        sidecar = NativeSidecarProcess(
            binary=Path("/usr/bin/false"),
            socket_root=self.socket_root,
            local_only=True,
            queue_capacity=1,
            startup_timeout=1.0,
        )
        if self.grace_seconds is not None:
            sidecar.socket_conflict_grace_seconds = self.grace_seconds
        sidecar._prepare_socket_root()
        return super().open()


def _owner(first: _Route, second: _Route) -> ColdRouteRestartOwner:
    routes = [first, second]
    created: list[_Route] = []

    def factory() -> _Route:
        route = routes[len(created)]
        created.append(route)
        return route

    def qualify(route: _Route, generation: int):
        return route.open(), f"qualification-{generation}-{route.name}"

    owner = ColdRouteRestartOwner(
        route_factory=factory, open_and_qualify=qualify, restart_budget=1
    )
    owner.open()
    return owner


@pytest.fixture
def short_root():
    # UDS paths are capped below 100 bytes; tmp_path is too long.
    root = Path(f"/private/tmp/mnr-{secrets.token_hex(4)}")
    root.mkdir(mode=0o700)
    try:
        yield root / "socket"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_restart_waits_for_exiting_old_sidecar_socket(short_root: Path) -> None:
    first = _SocketOwnerRoute("first", short_root, linger_seconds=1.0)
    second = _ReopeningRoute("second", short_root, grace_seconds=None)
    owner = _owner(first, second)
    first.hold_socket()
    started = time.monotonic()
    try:
        owner._restart_from(first, 1, reason="route_not_alive")
        status = owner.public_status()["cold_route_restart"]
        assert status["state"] == "ready", status
        assert status["generation"] == 2, status
        assert time.monotonic() - started >= 0.9  # it waited for the owner
        assert second.open_count == 1
    finally:
        first.release.set()
        owner.close()


def test_restart_still_refuses_live_foreign_socket_owner(short_root: Path) -> None:
    first = _SocketOwnerRoute("first", short_root, linger_seconds=30.0)
    second = _ReopeningRoute("second", short_root, grace_seconds=0.3)
    owner = _owner(first, second)
    first.hold_socket()
    try:
        with pytest.raises(ColdRouteRestartError) as raised:
            owner._restart_from(first, 1, reason="route_not_alive")
        assert COLD_ROUTE_RESTART_FAILED_CODE in str(raised.value)
        status = owner.public_status()["cold_route_restart"]
        assert status["state"] == "failed", status
        assert status["last_restart_reason"] == "sidecar_socket_conflict", status
        assert (short_root / "i.sock").exists()  # the live owner is untouched
    finally:
        first.release.set()
        owner.close()
