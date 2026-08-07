from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import mycelium_physical_runner
from mycelium_physical_runner import PhysicalRunner
from mycelium_physical_runner import adapters as runner_adapters
from mycelium_physical_runner import assembly as runner_assembly
from mycelium_physical_runner.adapters import (
    REQUIRED_AUTHORITY_DOCUMENTS,
    build_authority_publisher,
    build_seal_adapter,
)
from mycelium_physical_runner.config import parse_operator_plan
from mycelium_physical_runner.errors import RunnerError
from mycelium_physical_runner.state import RunnerState
from tests.physical_runner.conftest import operator_plan_payload


def _documents() -> dict[str, dict[str, Any]]:
    return {path: {"protocol": f"test.{index}.v1"} for index, path in enumerate(REQUIRED_AUTHORITY_DOCUMENTS)}


class _AssemblyController:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def seal_evidence(self) -> dict[str, str]:
        self.calls.append("seal-evidence")
        return {"command": "seal"}

    def execute(self, command: str) -> dict[str, str]:
        self.calls.append(command)
        return {"command": command}


def test_assembly_controller_uses_sealed_only_path_for_qualification() -> None:
    adapter_type = getattr(runner_assembly, "_RunnerControllerAdapter", None)
    assert adapter_type is not None
    controller = _AssemblyController()

    result = adapter_type(controller).execute("seal")

    assert result == {"command": "seal"}
    assert controller.calls == ["seal-evidence"]


def test_assembly_controller_delegates_non_seal_commands() -> None:
    adapter_type = getattr(runner_assembly, "_RunnerControllerAdapter", None)
    assert adapter_type is not None
    controller = _AssemblyController()

    result = adapter_type(controller).execute("run")

    assert result == {"command": "run"}
    assert controller.calls == ["run"]


def test_seal_adapter_requires_exact_documents_and_calls_real_sealer_once(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_sealer(**kwargs: Any) -> Any:
        calls.append(kwargs)
        root = tmp_path / kwargs["run_id"]
        root.mkdir()
        return SimpleNamespace(root=root, manifest_digest="sha256:" + "a" * 64)

    adapter = build_seal_adapter(
        output_dir=tmp_path,
        document_builder=lambda evidence: evidence["authority_documents"],
        seal_physical_evidence_fn=fake_sealer,
    )
    descriptor = adapter(run_id="run-1", evidence={"authority_documents": _documents()})

    assert len(calls) == 1
    assert tuple(calls[0]["documents"]) == REQUIRED_AUTHORITY_DOCUMENTS
    assert descriptor == {
        "run_id": "run-1",
        "manifest_path": str(tmp_path / "run-1" / "evidence-manifest.json"),
        "manifest_digest": "sha256:" + "a" * 64,
    }


def test_seal_adapter_never_fabricates_missing_documents(tmp_path: Path) -> None:
    called = False

    def fake_sealer(**_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("must not seal incomplete evidence")

    adapter = build_seal_adapter(
        output_dir=tmp_path,
        document_builder=lambda evidence: evidence,
        seal_physical_evidence_fn=fake_sealer,
    )
    incomplete = _documents()
    incomplete.pop(REQUIRED_AUTHORITY_DOCUMENTS[-1])

    with pytest.raises(RunnerError) as caught:
        adapter(run_id="run-1", evidence=incomplete)
    assert caught.value.code == "authority_documents_invalid"
    assert called is False


def test_runner_does_not_export_non_authoritative_qualifier_adapter() -> None:
    assert not hasattr(runner_adapters, "build_qualify_adapter")
    assert not hasattr(mycelium_physical_runner, "build_qualify_adapter")


def test_authority_publisher_drops_record_whose_sealed_identity_does_not_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published_id = "qualification-other"

    class FakeAuthority:
        def __init__(self) -> None:
            self.dropped: list[str] = []

        def current(self) -> None:
            return None

        def qualify_and_publish(self, **_kwargs: Any) -> object:
            class Record:
                qualification_id = published_id

            return Record()

        def drop(self, *, expected_qualification_id: str) -> bool:
            self.dropped.append(expected_qualification_id)
            return True

    authority = FakeAuthority()
    monkeypatch.setattr(
        "mycelium_physical_runner.adapters._read_sealed_evidence",
        lambda _root: ({}, {}),
    )
    monkeypatch.setattr(
        "mycelium_physical_runner.adapters.route_qualification_to_dict",
        lambda _record: {
            "protocol": "mycelium.route_qualification.v1",
            "qualification_id": published_id,
            "evidence_manifest_digest": "sha256:" + "f" * 64,
            "evidence_class": "physical_qualification",
            "route_ready": True,
            "reason_codes": [],
        },
    )
    publisher = build_authority_publisher(
        authority=authority,  # type: ignore[arg-type]
        verify_gossip_signature=lambda *_: True,
        verify_load_proof_signature=lambda *_: True,
    )
    sealed = {
        "run_id": "run-1",
        "manifest_path": str(tmp_path / "evidence-manifest.json"),
        "manifest_digest": "sha256:" + "b" * 64,
    }
    with pytest.raises(RunnerError) as caught:
        publisher(sealed)
    assert caught.value.code == "authority_publication_mismatch"
    assert authority.dropped == [published_id]


class _FakeController:
    def __init__(
        self,
        *,
        cleanup_error: Exception | None = None,
        cleanup_actions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.cleanup_error = cleanup_error
        self.cleanup_actions = cleanup_actions if cleanup_actions is not None else [
            {
                "protocol": "mycelium.controller_remote_cleanup_ack.v1",
                "node_id": "node-a",
                "staging_root": "/opt/mycelium/stage-a",
                "removed": True,
            },
            {
                "protocol": "mycelium.controller_remote_cleanup_ack.v1",
                "node_id": "node-b",
                "staging_root": "/opt/mycelium/stage-b",
                "removed": True,
            },
        ]

    def execute(self, command: str) -> dict[str, Any]:
        self.calls.append(command)
        if command == "run":
            return {
                "protocol": "mycelium.physical_controller_result.v1",
                "command": "run",
                "route_ready": False,
                "release_ready": False,
                "run_id": "run-0001",
            }
        if command == "seal":
            return {
                "protocol": "mycelium.physical_controller_result.v1",
                "command": "seal",
                "run_id": "run-0001",
                "qualifier_invocations": 0,
                "route_ready": False,
                "release_ready": False,
                "sealed_manifest": {
                    "run_id": "run-0001",
                    "manifest_path": "/private/tmp/run-0001/evidence-manifest.json",
                    "manifest_digest": "sha256:" + "b" * 64,
                },
            }
        if command == "cleanup":
            if self.cleanup_error is not None:
                raise self.cleanup_error
            return {
                "protocol": "mycelium.physical_controller_result.v1",
                "command": "cleanup",
                "route_ready": False,
                "release_ready": False,
                "actions": self.cleanup_actions,
            }
        raise AssertionError(command)


class _PrequalifiedController(_FakeController):
    def execute(self, command: str) -> dict[str, Any]:
        if command != "seal":
            return super().execute(command)
        self.calls.append(command)
        return {
            "protocol": "mycelium.physical_controller_result.v1",
            "command": "seal",
            "run_id": "run-0001",
            "qualifier_invocations": 1,
            "route_ready": True,
            "release_ready": False,
            "sealed_manifest": {
                "run_id": "run-0001",
                "manifest_path": "/private/tmp/run-0001/evidence-manifest.json",
                "manifest_digest": "sha256:" + "b" * 64,
            },
            "qualification": {
                "protocol": "mycelium.route_qualification.v1",
                "run_id": "run-0001",
                "manifest_digest": "sha256:" + "b" * 64,
                "evidence_class": "physical_qualification",
                "accepted": True,
                "route_ready": True,
                "reason_codes": [],
                "qualified_by": "mycelium_qualification.qualifier:RouteQualificationV1",
            },
        }


def _published_record(sealed: Any) -> dict[str, Any]:
    return {
        "protocol": "mycelium.route_qualification.v1",
        "qualification_id": "qualification-1",
        "evidence_manifest_digest": sealed["manifest_digest"],
        "evidence_class": "physical_qualification",
        "route_ready": True,
        "reason_codes": [],
        "qualified_by": "mycelium_qualification.qualifier:RouteQualificationV1",
    }


def _config(tmp_path: Path):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    return parse_operator_plan(operator_plan_payload(workspace))


def test_diagnostic_executes_run_once_cleans_up_and_never_publishes(tmp_path: Path) -> None:
    controller = _FakeController()
    published: list[object] = []
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=lambda *_args: published.append(object()),
    )

    outcome = runner.execute("diagnose")

    assert controller.calls == ["run", "cleanup"]
    assert published == []
    assert outcome["route_ready"] is False
    assert outcome["release_ready"] is False


def test_qualify_executes_seal_once_then_cleanup_then_publish(tmp_path: Path) -> None:
    controller = _FakeController()
    events: list[str] = []

    class Publisher:
        def __call__(self, sealed: Any) -> Any:
            assert controller.calls == ["seal", "cleanup"]
            events.append("publish")
            return _published_record(sealed)

        def revoke(self, qualification_id: str) -> bool:
            del qualification_id
            return True

    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=Publisher(),
    )
    outcome = runner.execute("qualify")

    assert controller.calls == ["seal", "cleanup"]
    assert events == ["publish"]
    assert outcome["route_ready"] is True
    assert outcome["release_ready"] is False


def test_qualification_outcome_projects_only_allowlisted_authority_fields(
    tmp_path: Path,
) -> None:
    controller = _FakeController()
    canary = "PRIVATE-AUTHORITY-CANARY"

    class Publisher:
        def __call__(self, sealed: Any) -> Any:
            return {
                **_published_record(sealed),
                "private_key": canary,
                "raw_evidence": {"operator": canary},
            }

        def revoke(self, qualification_id: str) -> bool:
            del qualification_id
            return True

    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=Publisher(),
    )

    outcome = runner.execute("qualify")

    assert outcome["published_qualification"] == _published_record(
        outcome["sealed_manifest"]
    )
    assert canary not in repr(outcome)


def test_runner_rejects_bool_as_zero_qualifier_invocations(tmp_path: Path) -> None:
    class BoolCountController(_FakeController):
        def execute(self, command: str) -> dict[str, Any]:
            result = super().execute(command)
            if command == "seal":
                result["qualifier_invocations"] = False
            return result

    published: list[object] = []
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=BoolCountController(),
        publisher=lambda *_args: published.append(object()),  # type: ignore[arg-type]
    )

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "controller_seal_invalid"
    assert published == []


def test_runner_rejects_controller_that_qualified_before_publication(tmp_path: Path) -> None:
    controller = _PrequalifiedController()
    published: list[object] = []
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=lambda *_args: published.append(object()),  # type: ignore[arg-type]
    )

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "controller_seal_invalid"
    assert controller.calls == ["seal", "cleanup"]
    assert published == []


def test_ready_state_write_failure_revokes_published_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _FakeController()

    class RevocablePublisher:
        def __init__(self) -> None:
            self.revoked: list[str] = []

        def __call__(self, sealed: Any) -> Any:
            return _published_record(sealed)

        def revoke(self, qualification_id: str) -> bool:
            self.revoked.append(qualification_id)
            return True

    publisher = RevocablePublisher()
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=publisher,
    )
    writes: list[bool] = []
    real_write = runner._state_store.write

    def fail_first_ready_write(document: Any) -> None:
        writes.append(document.route_ready)
        if document.route_ready and writes.count(True) == 1:
            raise RunnerError("state_write_failed")
        real_write(document)

    monkeypatch.setattr(runner._state_store, "write", fail_first_ready_write)

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "state_write_failed"
    assert publisher.revoked == ["qualification-1"]
    assert writes == [False, True, False]


def test_qualified_callback_failure_revokes_before_ready_state_is_visible(
    tmp_path: Path,
) -> None:
    controller = _FakeController()

    class RevocablePublisher:
        def __init__(self) -> None:
            self.revoked: list[str] = []

        def __call__(self, sealed: Any) -> Any:
            return _published_record(sealed)

        def revoke(self, qualification_id: str) -> bool:
            self.revoked.append(qualification_id)
            return True

    publisher = RevocablePublisher()
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=publisher,
    )
    observed_states: list[RunnerState] = []

    def fail_qualified_callback(state: RunnerState) -> None:
        if state is RunnerState.QUALIFIED:
            observed_states.append(runner.state)
            raise RuntimeError("callback failed")

    runner.on_state_change = fail_qualified_callback

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "state_callback_failed"
    assert observed_states == [RunnerState.UNREADY]
    assert publisher.revoked == ["qualification-1"]
    assert runner.state is RunnerState.FAILED


def test_failed_callback_cannot_block_post_publication_fail_closed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _FakeController()

    class RevocablePublisher:
        def __init__(self) -> None:
            self.revoked: list[str] = []

        def __call__(self, sealed: Any) -> Any:
            return _published_record(sealed)

        def revoke(self, qualification_id: str) -> bool:
            self.revoked.append(qualification_id)
            return True

    publisher = RevocablePublisher()
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=publisher,
    )
    real_write = runner._state_store.write
    failed_once = False

    def fail_ready_once(document: Any) -> None:
        nonlocal failed_once
        if document.route_ready is True and not failed_once:
            failed_once = True
            raise RunnerError("state_write_failed")
        real_write(document)

    def fail_failed_callback(state: RunnerState) -> None:
        if state is RunnerState.FAILED:
            raise RuntimeError("failed callback failed")

    monkeypatch.setattr(runner._state_store, "write", fail_ready_once)
    runner.on_state_change = fail_failed_callback

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "state_write_failed"
    assert publisher.revoked == ["qualification-1"]
    assert runner.state is RunnerState.FAILED
    persisted = runner._state_store.read()
    assert persisted is not None
    assert persisted["route_ready"] is False


def test_lock_release_failure_revokes_published_qualification(tmp_path: Path) -> None:
    controller = _FakeController()

    class RevocablePublisher:
        def __init__(self) -> None:
            self.revoked: list[str] = []

        def __call__(self, sealed: Any) -> Any:
            return _published_record(sealed)

        def revoke(self, qualification_id: str) -> bool:
            self.revoked.append(qualification_id)
            return True

    class FailingReleaseLock:
        held = False

        def acquire(self) -> None:
            self.held = True

        def release(self) -> None:
            self.held = False
            raise RunnerError("runner_lock_release_failed")

    publisher = RevocablePublisher()
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=publisher,
    )
    runner._lock = FailingReleaseLock()  # type: ignore[assignment]

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "runner_lock_release_failed"
    assert publisher.revoked == ["qualification-1"]
    assert runner.state is RunnerState.FAILED
    persisted = runner._state_store.read()
    assert persisted is not None
    assert persisted["route_ready"] is False


def test_lock_release_failure_prevents_ready_state_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _FakeController()

    class RevocablePublisher:
        def __init__(self) -> None:
            self.revoked: list[str] = []

        def __call__(self, sealed: Any) -> Any:
            return _published_record(sealed)

        def revoke(self, qualification_id: str) -> bool:
            self.revoked.append(qualification_id)
            return True

    class FailingReleaseLock:
        held = False

        def acquire(self) -> None:
            self.held = True

        def release(self) -> None:
            self.held = False
            raise RunnerError("runner_lock_release_failed")

    publisher = RevocablePublisher()
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=publisher,
    )
    runner._lock = FailingReleaseLock()  # type: ignore[assignment]
    real_write = runner._state_store.write
    failed_once = False

    def fail_ready_once(document: Any) -> None:
        nonlocal failed_once
        if document.route_ready is True and not failed_once:
            failed_once = True
            raise RunnerError("state_write_failed")
        real_write(document)

    monkeypatch.setattr(runner._state_store, "write", fail_ready_once)

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "runner_lock_release_failed"
    assert failed_once is False
    assert publisher.revoked == ["qualification-1"]
    assert runner.state is RunnerState.FAILED
    persisted = runner._state_store.read()
    assert persisted is not None
    assert persisted["route_ready"] is False


def test_failed_state_persistence_is_primary_when_revocation_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _FakeController()
    publication = {"complete": False}

    class NonRevocablePublisher:
        def __call__(self, sealed: Any) -> Any:
            record = _published_record(sealed)
            publication["complete"] = True
            return record

        def revoke(self, qualification_id: str) -> bool:
            assert qualification_id == "qualification-1"
            return False

    class FailingReleaseLock:
        held = False

        def acquire(self) -> None:
            self.held = True

        def release(self) -> None:
            self.held = False
            raise RunnerError("runner_lock_release_failed")

    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=NonRevocablePublisher(),
    )
    runner._lock = FailingReleaseLock()  # type: ignore[assignment]
    real_write = runner._state_store.write

    def fail_closed_write(document: Any) -> None:
        if publication["complete"] and document.route_ready is False:
            raise RunnerError("state_write_failed")
        real_write(document)

    monkeypatch.setattr(runner._state_store, "write", fail_closed_write)

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "runner_lock_release_failed"
    assert "state_write_failed" in getattr(caught.value, "__notes__", [])
    assert "authority_revoke_failed" in getattr(caught.value, "__notes__", [])
    assert runner.state is RunnerState.FAILED
    persisted = runner._state_store.read()
    assert persisted is not None
    assert persisted["route_ready"] is False


def test_original_state_callback_error_is_primary_when_revocation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _FakeController()
    publication = {"complete": False}

    class NonRevocablePublisher:
        def __call__(self, sealed: Any) -> Any:
            record = _published_record(sealed)
            publication["complete"] = True
            return record

        def revoke(self, qualification_id: str) -> bool:
            assert qualification_id == "qualification-1"
            return False

    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=NonRevocablePublisher(),
    )

    def fail_qualified_state(state: RunnerState) -> None:
        if state is RunnerState.QUALIFIED:
            raise RuntimeError("synthetic callback failure")

    real_write = runner._state_store.write

    def fail_closed_write(document: Any) -> None:
        if publication["complete"] and document.route_ready is False:
            raise RunnerError("state_write_failed")
        real_write(document)

    monkeypatch.setattr(runner._state_store, "write", fail_closed_write)
    runner.on_state_change = fail_qualified_state

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "state_callback_failed"
    assert "state_write_failed" in getattr(caught.value, "__notes__", [])
    assert "authority_revoke_failed" in getattr(caught.value, "__notes__", [])
    assert runner.state is RunnerState.FAILED
    persisted = runner._state_store.read()
    assert persisted is not None
    assert persisted["route_ready"] is False


def test_lock_release_error_is_primary_when_revocation_fails(
    tmp_path: Path,
) -> None:
    controller = _FakeController()

    class NonRevocablePublisher:
        def __call__(self, sealed: Any) -> Any:
            return _published_record(sealed)

        def revoke(self, qualification_id: str) -> bool:
            assert qualification_id == "qualification-1"
            return False

    class FailingReleaseLock:
        held = False

        def acquire(self) -> None:
            self.held = True

        def release(self) -> None:
            self.held = False
            raise RunnerError("runner_lock_release_failed")

    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=NonRevocablePublisher(),
    )
    runner._lock = FailingReleaseLock()  # type: ignore[assignment]

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "runner_lock_release_failed"
    assert "authority_revoke_failed" in getattr(caught.value, "__notes__", [])
    assert runner.state is RunnerState.FAILED
    persisted = runner._state_store.read()
    assert persisted is not None
    assert persisted["route_ready"] is False


def test_raw_lock_release_exception_is_fail_closed_after_publication(
    tmp_path: Path,
) -> None:
    controller = _FakeController()

    class RevocablePublisher:
        def __init__(self) -> None:
            self.revoked: list[str] = []

        def __call__(self, sealed: Any) -> Any:
            return _published_record(sealed)

        def revoke(self, qualification_id: str) -> bool:
            self.revoked.append(qualification_id)
            return True

    class RawFailingReleaseLock:
        held = False

        def acquire(self) -> None:
            self.held = True

        def release(self) -> None:
            self.held = False
            raise OSError("raw release failure")

    publisher = RevocablePublisher()
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=publisher,
    )
    runner._lock = RawFailingReleaseLock()  # type: ignore[assignment]

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "runner_lock_release_failed"
    assert publisher.revoked == ["qualification-1"]
    assert runner.state is RunnerState.FAILED
    persisted = runner._state_store.read()
    assert persisted is not None
    assert persisted["route_ready"] is False


def test_raw_failed_state_write_is_wrapped_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _FakeController()
    publication = {"complete": False}

    class RevocablePublisher:
        def __call__(self, sealed: Any) -> Any:
            record = _published_record(sealed)
            publication["complete"] = True
            return record

        def revoke(self, qualification_id: str) -> bool:
            assert qualification_id == "qualification-1"
            return True

    class FailingReleaseLock:
        held = False

        def acquire(self) -> None:
            self.held = True

        def release(self) -> None:
            self.held = False
            raise RunnerError("runner_lock_release_failed")

    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=RevocablePublisher(),
    )
    runner._lock = FailingReleaseLock()  # type: ignore[assignment]
    real_write = runner._state_store.write

    def raw_fail_closed_write(document: Any) -> None:
        if publication["complete"] and document.route_ready is False:
            raise OSError("raw state failure")
        real_write(document)

    monkeypatch.setattr(runner._state_store, "write", raw_fail_closed_write)

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "runner_lock_release_failed"
    assert "state_write_failed" in getattr(caught.value, "__notes__", [])
    assert runner.state is RunnerState.FAILED
    persisted = runner._state_store.read()
    assert persisted is not None
    assert persisted["route_ready"] is False


def test_failed_state_callback_cannot_reenter_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _FakeController()

    class CurrentPublisher:
        def __init__(self) -> None:
            self.counter = 0
            self.current_qualification_id: str | None = None
            self.revoked: list[str] = []

        def __call__(self, sealed: Any) -> Any:
            self.counter += 1
            qualification_id = f"qualification-{self.counter}"
            self.current_qualification_id = qualification_id
            record = _published_record(sealed)
            record["qualification_id"] = qualification_id
            return record

        def revoke(self, qualification_id: str) -> bool:
            if self.current_qualification_id != qualification_id:
                return False
            self.revoked.append(qualification_id)
            self.current_qualification_id = None
            return True

    publisher = CurrentPublisher()
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=publisher,
    )
    real_write = runner._state_store.write
    failed_ready_write = False

    def fail_first_ready_write(document: Any) -> None:
        nonlocal failed_ready_write
        if document.route_ready is True and not failed_ready_write:
            failed_ready_write = True
            raise RunnerError("state_write_failed")
        real_write(document)

    monkeypatch.setattr(runner._state_store, "write", fail_first_ready_write)
    nested_codes: list[str] = []
    attempted_reentry = False

    def reenter_on_failed(state: RunnerState) -> None:
        nonlocal attempted_reentry
        if state is not RunnerState.FAILED or attempted_reentry:
            return
        attempted_reentry = True
        try:
            runner.execute("qualify")
        except RunnerError as exc:
            nested_codes.append(exc.code)

    runner.on_state_change = reenter_on_failed

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")

    assert caught.value.code == "state_write_failed"
    assert nested_codes == ["runner_lock_held"]
    assert publisher.revoked == ["qualification-1"]
    assert publisher.current_qualification_id is None
    assert runner.state is RunnerState.FAILED
    persisted = runner._state_store.read()
    assert persisted is not None
    assert persisted["route_ready"] is False


def test_state_callback_cannot_release_lock_to_reenter_qualification(
    tmp_path: Path,
) -> None:
    controller = _FakeController()

    class CurrentPublisher:
        def __init__(self) -> None:
            self.counter = 0
            self.current_qualification_id: str | None = None

        def __call__(self, sealed: Any) -> Any:
            self.counter += 1
            qualification_id = f"qualification-{self.counter}"
            self.current_qualification_id = qualification_id
            record = _published_record(sealed)
            record["qualification_id"] = qualification_id
            return record

        def revoke(self, qualification_id: str) -> bool:
            if self.current_qualification_id != qualification_id:
                return False
            self.current_qualification_id = None
            return True

    publisher = CurrentPublisher()
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=publisher,
    )
    release_codes: list[str] = []
    nested_codes: list[str] = []
    attempted_reentry = False

    def release_then_reenter(state: RunnerState) -> None:
        nonlocal attempted_reentry
        if state is not RunnerState.QUALIFIED or attempted_reentry:
            return
        attempted_reentry = True
        try:
            runner.release_lock()
        except RunnerError as exc:
            release_codes.append(exc.code)
        try:
            runner.execute("qualify")
        except RunnerError as exc:
            nested_codes.append(exc.code)

    runner.on_state_change = release_then_reenter

    outcome = runner.execute("qualify")

    assert outcome["route_ready"] is True
    assert release_codes == ["runner_lock_held"]
    assert nested_codes == ["runner_lock_held"]
    assert publisher.counter == 1
    assert publisher.current_qualification_id == "qualification-1"
    assert runner.state is RunnerState.QUALIFIED
    persisted = runner._state_store.read()
    assert persisted is not None
    assert persisted["qualification_id"] == "qualification-1"
    assert persisted["route_ready"] is True


def test_cleanup_failure_blocks_publication_and_qualified_state(tmp_path: Path) -> None:
    controller = _FakeController(cleanup_error=RuntimeError("cleanup failed"))
    published: list[object] = []
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=lambda *_args: published.append(object()),
    )

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")
    assert caught.value.code == "controller_cleanup_failed"
    assert controller.calls == ["seal", "cleanup"]
    assert published == []
    assert runner.state.value == "failed"


def test_publication_failure_is_fatal_and_never_returns_ready(tmp_path: Path) -> None:
    controller = _FakeController()

    class FailingPublisher:
        def __call__(self, sealed: Any) -> Any:
            del sealed
            raise RuntimeError("authority refused")

        def revoke(self, qualification_id: str) -> bool:
            del qualification_id
            return True

    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=FailingPublisher(),
    )
    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")
    assert caught.value.code == "authority_publish_failed"
    assert controller.calls == ["seal", "cleanup"]
    assert runner.state.value == "failed"


def test_incomplete_cleanup_ack_blocks_publication(tmp_path: Path) -> None:
    controller = _FakeController(cleanup_actions=[])
    published: list[object] = []
    runner = PhysicalRunner(
        config=_config(tmp_path),
        controller=controller,
        publisher=lambda *_args: published.append(object()),
    )

    with pytest.raises(RunnerError) as caught:
        runner.execute("qualify")
    assert caught.value.code == "controller_cleanup_invalid"
    assert controller.calls == ["seal", "cleanup"]
    assert published == []
    assert runner.state.value == "failed"
