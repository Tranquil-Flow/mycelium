# SPDX-License-Identifier: AGPL-3.0-only
"""Missing optional artifact skips; broken explicitly selected artifacts fail."""
import pytest
import test_package


def test_absent_package_skips_with_actionable_reason(monkeypatch):
    monkeypatch.delenv('A13_PACKAGE_EXECUTABLE', raising=False)
    with pytest.raises(pytest.skip.Exception, match='A13_PACKAGE_EXECUTABLE'):
        test_package.built_package_required.__wrapped__()


@pytest.mark.parametrize('configured', ['', '/missing/package/executable'])
def test_explicit_invalid_package_is_not_silently_skipped(monkeypatch, configured):
    monkeypatch.setenv('A13_PACKAGE_EXECUTABLE', configured)
    with pytest.raises(AssertionError, match='configured_package_executable_missing'):
        test_package.built_package_required.__wrapped__()


def test_valid_package_enters_test_body(monkeypatch, tmp_path):
    executable = tmp_path / 'fixture executable'
    executable.write_text('not executed by this unit test')
    executable.chmod(0o755)
    monkeypatch.setenv('A13_PACKAGE_EXECUTABLE', str(executable))
    test_package.built_package_required.__wrapped__()
    executable.chmod(0o644)
    with pytest.raises(AssertionError, match='not_executable'):
        test_package.built_package_required.__wrapped__()
