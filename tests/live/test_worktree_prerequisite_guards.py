# SPDX-License-Identifier: AGPL-3.0-only
"""Native prerequisite guards cannot hide a broken installed build."""
import pytest
from tests.worktree_prerequisites import require_native


def test_missing_native_input_skips_with_setup_reason(tmp_path):
    with pytest.raises(pytest.skip.Exception, match='worktree_test_setup.sh'):
        require_native(tmp_path / 'absent', 'test native')


@pytest.mark.parametrize('kind', ['directory', 'nonexecutable', 'dangling_symlink'])
def test_present_broken_native_input_fails_instead_of_skipping(tmp_path, kind):
    path = tmp_path / 'broken'
    if kind == 'directory':
        path.mkdir()
    elif kind == 'nonexecutable':
        path.write_text('fixture, never executed')
        path.chmod(0o644)
    else:
        path.symlink_to(tmp_path / 'missing-target')
    with pytest.raises(AssertionError, match='present but not an executable file'):
        require_native(path, 'test native')


def test_executable_native_input_allows_original_test_to_run(tmp_path):
    path = tmp_path / 'native'
    path.write_text('fixture, never executed')
    path.chmod(0o755)
    assert require_native(path, 'test native') == path
