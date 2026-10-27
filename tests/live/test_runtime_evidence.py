# SPDX-License-Identifier: AGPL-3.0-only
"""Evidence destinations must not require JUnit or overwrite another run."""
import pytest
from test_runtime_lifecycle import _evidence_directory


@pytest.mark.parametrize('mixed', [False, True])
@pytest.mark.parametrize('with_junit', [False, True])
def test_lifecycle_evidence_is_private_per_invocation(tmp_path, mixed, with_junit):
    junit = tmp_path / 'reports' / 'junit.xml' if with_junit else None
    fallback = tmp_path / 'private-test'
    first = _evidence_directory(junit, fallback, mixed)
    sentinel = first / 'keep.txt'
    sentinel.write_text('prior evidence')
    second = _evidence_directory(junit, fallback, mixed)
    assert first != second
    assert first.is_dir() and second.is_dir()
    assert sentinel.read_text() == 'prior evidence'
    assert first.parent == (junit.parent if with_junit else fallback) / 'runtime-emitted'
    assert first.name.startswith('mixed-' if mixed else 'desktop-')
