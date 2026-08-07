"""Run state is atomic, private, canonical, and free of credential material."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from mycelium_physical_runner.errors import RunnerError
from mycelium_physical_runner.state import RunnerState, RunStateDocument
from mycelium_physical_runner.state_store import StateStore

SECRET_VALUE = "hf_" + "S" * 40


def _document(**overrides: object) -> RunStateDocument:
    values: dict[str, object] = {
        "plan_id": "two-mac-g4",
        "run_id": "run-0001",
        "operator_plan_path": "/opt/mycelium/plans/two-mac.json",
        "command": "qualify",
        "state": RunnerState.QUALIFIED,
        "updated_at_unix_ms": 1_800_000_000_000,
        "route_ready": True,
        "manifest_digest": "sha256:" + "c" * 64,
        "qualification_id": "qualification-1",
    }
    values.update(overrides)
    return RunStateDocument(**values)  # type: ignore[arg-type]


def test_state_is_written_atomically_with_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "run" / "state.json"
    store = StateStore(state_path=path)

    store.write(_document())

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not list(path.parent.glob("*.tmp"))
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["protocol"] == "mycelium.physical_runner_state.v1"
    assert document["state"] == "qualified"
    assert document["route_ready"] is True


def test_state_bytes_are_canonical_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(state_path=path)

    store.write(_document())
    first = path.read_bytes()
    store.write(_document())

    assert path.read_bytes() == first
    assert first == json.dumps(
        json.loads(first.decode("utf-8")), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def test_state_never_carries_secret_or_controller_payloads(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    StateStore(state_path=path).write(_document())

    raw = path.read_bytes()
    assert SECRET_VALUE.encode("utf-8") not in raw
    assert b"BEGIN" not in raw
    assert set(json.loads(raw.decode("utf-8"))) == {
        "protocol",
        "plan_id",
        "run_id",
        "operator_plan_path",
        "command",
        "state",
        "updated_at_unix_ms",
        "route_ready",
        "manifest_digest",
        "qualification_id",
    }


def test_replace_failure_is_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    store = StateStore(state_path=path)

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("replace refused")

    monkeypatch.setattr(os, "replace", _fail)
    with pytest.raises(RunnerError) as caught:
        store.write(_document())
    assert caught.value.code == "state_write_failed"
    assert not path.exists()


def test_chmod_failure_is_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    store = StateStore(state_path=path)

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("chmod refused")

    monkeypatch.setattr(os, "chmod", _fail)
    with pytest.raises(RunnerError) as caught:
        store.write(_document())
    assert caught.value.code == "state_write_failed"


def test_read_rejects_symlinks_and_corruption(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(state_path=path)
    assert store.read() is None

    store.write(_document())
    assert store.read()["run_id"] == "run-0001"

    path.chmod(0o600)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RunnerError) as corrupt:
        store.read()
    assert corrupt.value.code == "state_corrupt"

    linked = tmp_path / "linked.json"
    linked.symlink_to(path)
    with pytest.raises(RunnerError) as symlink:
        StateStore(state_path=linked).read()
    assert symlink.value.code == "state_unavailable"
