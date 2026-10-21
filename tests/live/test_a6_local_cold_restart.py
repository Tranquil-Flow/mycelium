# SPDX-License-Identifier: AGPL-3.0-only
"""Cold route restart after a SIGKILLed source peer, on real local processes."""
from __future__ import annotations

import faulthandler
import os
import signal
import sys
import threading
import time
import uuid

import pytest

from mycelium_live.supervisor import ColdRouteRestartOwner
from mycelium_router.contracts import PathHop, PathManifest
from mycelium_router.serialization import path_manifest_to_dict
from tests.live._a6_local_physical import (
    MAX_NEW,
    PROMPT,
    build_local_physical,
    synthetic_open_and_qualify,
)
from tests.physical_qualification.test_node_service import SIDECAR_BINARY

pytestmark = pytest.mark.skipif(
    not SIDECAR_BINARY.is_file(), reason="native iroh sidecar not built"
)

_QUALIFICATION_DIGEST = "sha256:" + "7" * 64


def _identity(graph, request_id, placement_ids):
    import hashlib
    import json

    manifest = PathManifest(
        path_id=f"a6-local-{request_id}",
        path_attempt=1,
        request_id=request_id,
        deployment_id=graph.deployment_id,
        deployment_epoch=graph.deployment_epoch,
        topology_version=graph.topology_version,
        manifest_digest=graph.manifest_digest,
        ordered_hops=tuple(
            PathHop(
                stage_id=stage.stage_id,
                placement_id=placement_id,
                reservation_id=f"reservation-{request_id}-{index}",
                reservation_expires_at=1e15,
                reservation_epoch=1,
            )
            for index, (stage, placement_id) in enumerate(zip(graph.stages, placement_ids))
        ),
        loopback_edge_id=next(
            edge.edge_id for edge in graph.loopback_edges
            if edge.from_placement_id == placement_ids[-1]
            and edge.to_placement_id == placement_ids[0]
        ),
    )
    document = path_manifest_to_dict(manifest)
    digest = "sha256:" + hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    route_identity = {
        "request_id": request_id, "request_attempt": 1, "path_id": document["path_id"],
        "path_attempt": document["path_attempt"], "path_manifest_digest": digest,
        "deployment_id": graph.deployment_id, "deployment_epoch": graph.deployment_epoch,
        "topology_generation": graph.topology_version,
    }
    command_identity = {
        "deployment_id": graph.deployment_id, "deployment_epoch": graph.deployment_epoch,
        "qualification_digest": _QUALIFICATION_DIGEST, "request_id": request_id,
        "request_attempt": 1, "path_id": document["path_id"],
        "path_attempt": document["path_attempt"], "path_digest": digest,
        "topology_generation": graph.topology_version,
        "command_id": f"owner-command-{request_id}", "publisher_generation": 1,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 600_000,
        "cancellation_generation": 0,
    }
    return document, route_identity, command_identity


class _KillAt:
    def __init__(self, local, node_id, at_index=0):
        self.local, self.node_id, self.at_index = local, node_id, at_index
        self.tokens = []
        self.killed = False

    def emit(self, token_index, token_id):
        self.tokens.append((token_index, token_id))
        if token_index == self.at_index and not self.killed:
            session = self.local.session_factory.sessions[self.node_id]
            os.killpg(os.getpgid(session._process.pid), signal.SIGKILL)
            self.killed = True
            deadline = time.monotonic() + 10
            while session.returncode is None and time.monotonic() < deadline:
                time.sleep(0.02)


def test_cold_restart_after_entry_source_kill_reopens_the_route(tmp_path, monkeypatch) -> None:
    import mycelium_request_gateway.contracts as gateway_contracts

    monkeypatch.setattr(
        gateway_contracts, "qualification_digest", lambda _q: _QUALIFICATION_DIGEST)
    local = build_local_physical(tmp_path, shape="single")
    owner = ColdRouteRestartOwner(
        route_factory=local.new_route,
        open_and_qualify=synthetic_open_and_qualify,
        restart_budget=1,
    )
    faulthandler.dump_traceback_later(240, exit=False, file=sys.stderr)
    try:
        owner.open()
        first = owner._current
        request_id = "a6restart" + uuid.uuid4().hex[:16]
        placements = tuple(stage.placements[0].placement_id for stage in local.graph.stages)
        manifest, route_identity, command_identity = _identity(
            local.graph, request_id, placements)
        sink = _KillAt(local, "node-a", at_index=0)
        receipts = []
        with pytest.raises(Exception):
            owner.infer(
                PROMPT, max_new_tokens=MAX_NEW, request_id=request_id, sink=sink,
                route_identity=route_identity, locked_path_manifest=manifest,
                command_identity=command_identity,
                authorize_cleanup=lambda deadline: deadline,
                publish_cleanup_receipt=lambda receipt: receipts.append(dict(receipt)),
            )
        assert sink.killed
        assert first.is_alive() is False
        started = time.monotonic()
        timings = {}

        def release():
            owner.release_request(request_id)
            timings["release_returned_s"] = time.monotonic() - started

        thread = threading.Thread(target=release, daemon=True)
        thread.start()
        thread.join(timeout=180)
        # The release no longer holds the caller for the replacement's open
        # (the failed terminal must not wait for it); wait for the restart.
        restart_deadline = time.monotonic() + 180
        while (
            owner.public_status()["cold_route_restart"]["state"] == "restarting"
            and time.monotonic() < restart_deadline
        ):
            time.sleep(0.1)
        status = owner.public_status()["cold_route_restart"]
        sys.stderr.write(f"\nRESTART timings={timings} status={status}\n")
        assert not thread.is_alive(), "cold restart did not finish within 180 s"
        assert status["state"] == "ready", status
        assert status["generation"] == 2
        assert owner._current is not first
        assert owner._current.is_alive()
    finally:
        faulthandler.cancel_dump_traceback_later()
        owner.close()
        local.close()
