from __future__ import annotations

from types import SimpleNamespace

from mycelium_live.route import PhysicalLiveRoute


def test_physical_route_projects_only_validated_assignment_and_load_bindings() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._membership_snapshot = {
        "assignment_offers": [
            {
                "message": {
                    "assignment_id": "assignment-a",
                    "recipient_node_id": "node-a",
                    "generation": 4,
                    "load_generation": 17,
                    "assignment_digest": "sha256:" + "a" * 64,
                    "stage_pack_digest": "sha256:" + "b" * 64,
                }
            }
        ]
    }
    route._graph = SimpleNamespace(
        stages=(
            SimpleNamespace(
                stage_id="stage-a",
                placements=(
                    SimpleNamespace(
                        assignment_id="assignment-a",
                        placement_id="stage-a-primary",
                        load_proof_digest="sha256:" + "c" * 64,
                    ),
                ),
            ),
        )
    )

    assert route.product_assignment_records() == (
        {
            "assignment_id": "assignment-a",
            "node_id": "node-a",
            "stage_id": "stage-a-primary",
            "membership_generation": 4,
            "load_generation": 17,
            "assignment_digest": "sha256:" + "a" * 64,
            "stage_pack_digest": "sha256:" + "b" * 64,
            "load_proof_digest": "sha256:" + "c" * 64,
        },
    )


def test_active_member_revocation_fences_and_closes_the_physical_route(
    monkeypatch,
) -> None:
    from collections import deque
    import threading

    import mycelium_live.route as route_module

    events: list[str] = []

    class Session:
        returncode: int | None = None

        def send(self, *, command_id: str, command: str, payload: dict) -> None:
            assert command_id and payload == {}
            events.append(command)

        def close(self) -> None:
            events.append("session_closed")
            self.returncode = 0

    class Authority:
        state_root = SimpleNamespace(path="/owner/seed")

        def current_members(self):
            return ({"node_id": "node-a", "generation": 7},)

        def close(self) -> None:
            events.append("authority_closed")

    revoked = {
        "node_id": "node-a",
        "generation": 8,
        "lifecycle_state": "STOPPED",
    }

    def revoke(state_root, *, node_id, expected_generation, reason):
        assert (state_root, node_id, expected_generation, reason) == (
            "/owner/seed",
            "node-a",
            7,
            "product-ui-owner-revocation",
        )
        events.append("revoked")
        return revoked

    monkeypatch.setattr(route_module, "revoke_seed_member", revoke)
    route = object.__new__(PhysicalLiveRoute)
    route._seed_authority = Authority()
    route._graph = SimpleNamespace(
        stages=(SimpleNamespace(placements=(SimpleNamespace(node_id="node-a"),)),)
    )
    route._plan = {"deployment_id": "deployment-a"}
    route._sessions = {"node-a": Session()}
    route._open = True
    route._closed = False
    route._fatal = None
    route._lock = threading.RLock()
    route._liveness_monitor_stop = threading.Event()
    route._liveness_monitor_thread = None
    route._incidents = deque(maxlen=64)
    route._incident_sequence = 0
    route._command_sequence = 0
    route._request_locks = {}

    assert route.revoke_native_member("node-a") == revoked

    assert events == ["revoked", "stop", "session_closed", "authority_closed"]
    assert route._fatal == "active_member_revoked"
    assert route._closed is True
    assert route.is_alive() is False
    import pytest

    with pytest.raises(RuntimeError, match="route_not_open"):
        route.infer(
            [1],
            max_new_tokens=1,
            request_id="after-revocation",
            sink=lambda *args: None,
        )
    assert route._incidents[-1]["state"] == "fatal"
    assert route._incidents[-1]["reason"] == "active_member_revoked"


def test_standby_member_revocation_does_not_close_the_active_route(monkeypatch) -> None:
    from collections import deque
    import threading

    import mycelium_live.route as route_module

    class Session:
        returncode: int | None = None

    class Authority:
        state_root = SimpleNamespace(path="/owner/seed")

        def current_members(self):
            return ({"node_id": "standby", "generation": 3},)

    monkeypatch.setattr(
        route_module,
        "revoke_seed_member",
        lambda *args, **kwargs: {
            "node_id": "standby",
            "generation": 4,
            "lifecycle_state": "STOPPED",
        },
    )
    route = object.__new__(PhysicalLiveRoute)
    route._seed_authority = Authority()
    route._graph = SimpleNamespace(
        stages=(SimpleNamespace(placements=(SimpleNamespace(node_id="node-a"),)),)
    )
    route._plan = {"deployment_id": "deployment-a"}
    route._sessions = {"node-a": Session()}
    route._open = True
    route._closed = False
    route._fatal = None
    route._lock = threading.RLock()
    route._liveness_monitor_stop = threading.Event()
    route._liveness_monitor_thread = None
    route._incidents = deque(maxlen=64)
    route._incident_sequence = 0

    result = route.revoke_native_member("standby")

    assert result["generation"] == 4
    assert route.is_alive() is True
    assert route._incidents == deque(maxlen=64)
