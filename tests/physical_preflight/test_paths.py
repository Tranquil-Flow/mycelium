from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from conftest import ROOT, canonical_bytes, refresh_authorization


def _validate(plan: dict[str, object], *, root: Path = ROOT) -> dict[str, object]:
    from mycelium_physical_preflight import validate_and_generate

    return validate_and_generate(canonical_bytes(plan), source_tree_root=root)


def _assert_code(plan: dict[str, object], code: str, *, root: Path = ROOT) -> None:
    from mycelium_physical_preflight import PreflightValidationError

    with pytest.raises(PreflightValidationError, match=code):
        _validate(plan, root=root)


def test_rejects_relative_noncanonical_shallow_and_source_tree_staging_paths(
    plan: dict[str, object],
) -> None:
    hosts = plan["hosts"]
    assert isinstance(hosts, list)

    hosts[0]["staging_root"] = "relative/stage"
    refresh_authorization(plan)
    _assert_code(plan, "path_not_absolute")

    hosts[0]["staging_root"] = "/Users/operator_a/../operator_a/stage"
    refresh_authorization(plan)
    _assert_code(plan, "path_not_canonical")

    hosts[0]["staging_root"] = "/"
    refresh_authorization(plan)
    _assert_code(plan, "unsafe_staging_path")

    hosts[0]["staging_root"] = str(ROOT / "stage")
    refresh_authorization(plan)
    _assert_code(plan, "source_tree_path")


def test_rejects_equal_or_ancestor_descendant_staging_roots(plan: dict[str, object]) -> None:
    hosts = plan["hosts"]
    assert isinstance(hosts, list)
    first = hosts[0]["staging_root"]

    hosts[1]["staging_root"] = first
    hosts[1]["token_file_path"] = first + "/.credentials/peer.token"
    refresh_authorization(plan)
    _assert_code(plan, "overlapping_staging_roots")

    hosts[1]["staging_root"] = first + "/nested"
    hosts[1]["token_file_path"] = first + "/nested/.credentials/peer.token"
    refresh_authorization(plan)
    _assert_code(plan, "overlapping_staging_roots")


def test_rejects_existing_symlink_component_without_following_it(
    plan: dict[str, object], tmp_path: Path
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    hosts = plan["hosts"]
    assert isinstance(hosts, list)
    hosts[0]["staging_root"] = str(link / "mycelium-physical-qualification" / "run")
    hosts[0]["token_file_path"] = str(
        link / "mycelium-physical-qualification" / "run" / ".credentials" / "coordinator.token"
    )
    refresh_authorization(plan)

    source_root = tmp_path / "unrelated-source"
    source_root.mkdir()
    _assert_code(plan, "symlink_path_component", root=source_root)


def test_token_files_must_be_strict_descendants_and_copyback_must_not_overlap(
    plan: dict[str, object],
) -> None:
    hosts = plan["hosts"]
    assert isinstance(hosts, list)

    hosts[0]["token_file_path"] = "/Users/operator_a/outside/token"
    refresh_authorization(plan)
    _assert_code(plan, "token_file_outside_staging")

    hosts[0]["token_file_path"] = hosts[0]["staging_root"] + "/.credentials/coordinator.token"
    hosts[0]["evidence_copyback_destination"] = hosts[0]["staging_root"] + "/evidence"
    refresh_authorization(plan)
    _assert_code(plan, "copyback_staging_overlap")


def test_staging_paths_require_dedicated_plan_namespace(plan: dict[str, object]) -> None:
    hosts = plan["hosts"]
    assert isinstance(hosts, list)
    hosts[0]["staging_root"] = "/Users/operator_a/arbitrary/place"
    refresh_authorization(plan)

    _assert_code(plan, "unsafe_staging_path")


def test_rejects_missing_empty_and_wrong_user_staging_roots(plan: dict[str, object]) -> None:
    import copy

    hosts = plan["hosts"]
    assert isinstance(hosts, list)

    missing = copy.deepcopy(plan)
    missing["hosts"][0].pop("staging_root")
    _assert_code(missing, "missing_field")

    hosts[0]["staging_root"] = ""
    refresh_authorization(plan)
    _assert_code(plan, "invalid_path")

    hosts[0]["staging_root"] = (
        "/Users/different_user/mycelium-physical-qualification/"
        "two-mac-route-qualification-001/m4pro"
    )
    hosts[0]["token_file_path"] = (
        hosts[0]["staging_root"] + "/.credentials/coordinator.token"
    )
    refresh_authorization(plan)
    _assert_code(plan, "staging_user_mismatch")


def test_source_descriptors_close_when_metadata_checks_fail(
    plan: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import mycelium_physical_preflight.validator as validator

    real_open = validator.os.open
    real_close = validator.os.close
    real_fstat = validator.os.fstat
    opened: set[int] = set()

    def tracked_open(*args: Any, **kwargs: Any) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        opened.discard(descriptor)
        real_close(descriptor)

    def fail_regular_fstat(descriptor: int):
        metadata = real_fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode):
            raise OSError("injected metadata failure")
        return metadata

    monkeypatch.setattr(validator.os, "open", tracked_open)
    monkeypatch.setattr(validator.os, "close", tracked_close)
    monkeypatch.setattr(validator.os, "fstat", fail_regular_fstat)

    try:
        with pytest.raises(validator.PreflightValidationError, match="source_file_not_regular"):
            validator.validate_and_generate(canonical_bytes(plan), source_tree_root=ROOT)
        assert opened == set()
    finally:
        for descriptor in tuple(opened):
            real_close(descriptor)
            opened.discard(descriptor)


def test_rejects_source_tree_descriptor_failure_without_leaking(
    plan: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    import mycelium_physical_preflight.validator as validator

    real_open = validator.os.open
    real_close = validator.os.close
    opened: set[int] = set()

    def tracked_open(*args: Any, **kwargs: Any) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        opened.discard(descriptor)
        real_close(descriptor)

    def fail_fstat(_descriptor: int):
        raise OSError("injected root failure")

    monkeypatch.setattr(validator.os, "open", tracked_open)
    monkeypatch.setattr(validator.os, "close", tracked_close)
    monkeypatch.setattr(validator.os, "fstat", fail_fstat)

    try:
        with pytest.raises(validator.PreflightValidationError, match="invalid_source_tree"):
            validator.validate_and_generate(canonical_bytes(plan), source_tree_root=ROOT)
        assert opened == set()
    finally:
        for descriptor in tuple(opened):
            real_close(descriptor)
            opened.discard(descriptor)
