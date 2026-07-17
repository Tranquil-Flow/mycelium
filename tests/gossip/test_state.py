from __future__ import annotations

import json
import multiprocessing as mp
import stat
from pathlib import Path

import pytest

from mycelium_gossip.schema import RecordKind
from mycelium_gossip.state import NodeStateError, NodeStateInUse, open_node_state
from tests.gossip.helpers import link_payload, offering_payload, profile_payload


def _try_open_locked(path: str, output) -> None:
    try:
        session = open_node_state(path)
    except Exception as exc:
        output.put((type(exc).__name__, str(exc)))
    else:
        session.close()
        output.put(("opened", ""))


def test_identity_is_stable_and_incarnation_increments_before_each_start(tmp_path: Path) -> None:
    path = tmp_path / "private" / "node-state.json"
    with open_node_state(path) as first:
        node_id = first.node_id
        assert first.incarnation == 1
        assert first.boot_id.startswith("boot-")
    with open_node_state(path) as second:
        assert second.node_id == node_id
        assert second.incarnation == 2
        assert second.boot_id != first.boot_id

    stored = json.loads(path.read_text())
    assert stored == {"incarnation": 2, "node_id": node_id, "version": 1}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_first_build_record_uses_protocol_sequence_zero(tmp_path: Path) -> None:
    path = tmp_path / "node-state.json"
    with open_node_state(path, node_id="node-a") as session:
        record = session.build_record(
            swarm_id="swarm-a",
            kind=RecordKind.PROFILE,
            payload=profile_payload("node-a"),
            ttl_ms=60_000,
            generated_at_unix_ms=1,
        )

    assert record.sequence == 0
    assert record.incarnation == 1


def test_sequences_are_independent_and_restart_at_zero_per_incarnation(tmp_path: Path) -> None:
    path = tmp_path / "node-state.json"
    with open_node_state(path, node_id="node-a") as session:
        assert session.next_sequence(RecordKind.PROFILE, profile_payload("node-a")) == 0
        assert session.next_sequence(RecordKind.PROFILE, profile_payload("node-a")) == 1
        assert session.next_sequence(RecordKind.LINK, link_payload("node-a", "node-b")) == 0
        assert session.next_sequence(RecordKind.OFFERING, offering_payload("node-a")) == 0
        record = session.build_record(
            swarm_id="swarm-a",
            kind=RecordKind.PROFILE,
            payload=profile_payload("node-a"),
            ttl_ms=60_000,
            generated_at_unix_ms=1,
        )
        assert record.sequence == 2
        assert record.incarnation == 1
        assert record.boot_id == session.boot_id

    with open_node_state(path, node_id="node-a") as restarted:
        assert restarted.incarnation == 2
        assert restarted.next_sequence(RecordKind.PROFILE, profile_payload("node-a")) == 0


def test_corrupt_state_fails_closed_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "node-state.json"
    path.write_text("not-json")

    with pytest.raises(NodeStateError, match="corrupt"):
        open_node_state(path)

    assert path.read_text() == "not-json"


def test_requested_node_id_cannot_replace_existing_identity(tmp_path: Path) -> None:
    path = tmp_path / "node-state.json"
    with open_node_state(path, node_id="node-a"):
        pass
    with pytest.raises(NodeStateError, match="does not match"):
        open_node_state(path, node_id="node-b")


def test_symlink_state_path_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"version":1,"node_id":"node-a","incarnation":1}')
    alias = tmp_path / "node-state.json"
    alias.symlink_to(target)

    with pytest.raises(NodeStateError, match="symlink"):
        open_node_state(alias)


def test_second_process_cannot_use_same_identity_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "node-state.json"
    with open_node_state(path, node_id="node-a"):
        ctx = mp.get_context("spawn")
        output = ctx.Queue()
        process = ctx.Process(target=_try_open_locked, args=(str(path), output))
        process.start()
        process.join(5.0)
        assert process.exitcode == 0
        result = output.get(timeout=1.0)

    assert result[0] == NodeStateInUse.__name__


def test_invalid_custom_node_id_rejected_before_file_creation(tmp_path: Path) -> None:
    path = tmp_path / "node-state.json"
    with pytest.raises(NodeStateError, match="node_id"):
        open_node_state(path, node_id="contains/slash")
    assert not path.exists()
