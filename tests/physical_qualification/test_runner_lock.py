from __future__ import annotations

import stat
from pathlib import Path

import pytest

from mycelium_physical_runner.errors import RunnerError
from mycelium_physical_runner.lock import ExclusiveLock


def test_exclusive_lock_blocks_second_process_handle_and_is_private(tmp_path: Path) -> None:
    path = tmp_path / "run" / "runner.lock"
    first = ExclusiveLock(path)
    second = ExclusiveLock(path)

    first.acquire()
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(RunnerError) as caught:
            second.acquire()
        assert caught.value.code == "runner_lock_held"
    finally:
        first.release()

    second.acquire()
    second.release()


def test_lock_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("sentinel", encoding="utf-8")
    link = tmp_path / "runner.lock"
    link.symlink_to(target)

    with pytest.raises(RunnerError) as caught:
        ExclusiveLock(link).acquire()
    assert caught.value.code == "runner_lock_unavailable"
    assert target.read_text(encoding="utf-8") == "sentinel"


def test_lock_release_is_idempotent(tmp_path: Path) -> None:
    lock = ExclusiveLock(tmp_path / "runner.lock")
    lock.acquire()
    lock.release()
    lock.release()
    assert lock.held is False


def test_lock_parent_is_owner_only_and_symlink_parent_is_rejected(tmp_path: Path) -> None:
    private_parent = tmp_path / "private"
    lock = ExclusiveLock(private_parent / "runner.lock")
    lock.acquire()
    lock.release()
    assert stat.S_IMODE(private_parent.stat().st_mode) == 0o700

    target = tmp_path / "target-parent"
    target.mkdir()
    linked = tmp_path / "linked-parent"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(RunnerError) as caught:
        ExclusiveLock(linked / "runner.lock").acquire()
    assert caught.value.code == "runner_lock_unavailable"
