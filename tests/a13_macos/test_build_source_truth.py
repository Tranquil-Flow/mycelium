# SPDX-License-Identifier: AGPL-3.0-only
"""The build must take its sources from THIS tree, and must say so in its own artefact.

WHY. On 2026-09-19 the committed PyInstaller spec pointed at a second checkout
(`mycelium-a13-macos-demo`) which held only a stale copy of the app package -- untracked, and absent from
that repository's entire history. No script invoked the spec, so no bundle was ever built from it, but
nothing prevented one either, and the failure it produces is invisible: a bundle built from a stale tree
still matches its own digests, so a LOCAL == REMOTE compare proves the copy and never the content. These
tests fail if the spec can point outside its own tree, if the build root stops looking like the app tree,
or if the identity artefact stops recording where the sources came from.
"""
import hashlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
import build_a13_macos  # noqa: E402


def test_the_spec_builds_the_tree_it_lives_in():
    lines = (REPO / 'Mycelium A13 Demo.spec').read_text().splitlines()
    code = '\n'.join(line for line in lines if not line.lstrip().startswith('#'))
    assert 'mycelium-a13-macos-demo' not in code, 'the spec points at a foreign checkout'
    assert '/Users/' not in code, 'the spec hardcodes an absolute checkout path'
    assert 'SPECPATH' in code, 'the spec does not resolve its own directory at build time'
    assert 'pathex=[ROOT]' in code, 'the spec does not put its own tree on the import path'
    assert "os.path.join(APP, '__main__.py')" in code


def test_no_script_drives_the_committed_spec():
    """build_a13_macos.py passes its own PyInstaller arguments; the spec must stay inert."""
    hits = [str(path.relative_to(REPO)) for path in (REPO / 'scripts').rglob('*.py')
            if 'Mycelium A13 Demo.spec' in path.read_text(errors='replace')]
    assert hits == [], 'something drives the committed spec: %r' % (hits,)


def test_the_build_root_must_look_like_the_app_tree():
    assert build_a13_macos.require_app_tree_root() == {'root': str(REPO)}


def test_a_foreign_build_root_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(build_a13_macos, 'ROOT', tmp_path)
    with pytest.raises(SystemExit, match='build_root_not_the_app_tree'):
        build_a13_macos.require_app_tree_root()


def test_the_compiled_but_unshipped_modules_are_recorded():
    """mycelium_live owns the route/serving state machine and never appears in a13-runtime-source."""
    digests = build_a13_macos.identity_of_compiled_modules()
    assert 'mycelium_live/route.py' in digests and 'mycelium_live/supervisor.py' in digests
    assert digests['mycelium_live/route.py'] == hashlib.sha256(
        (REPO / 'mycelium_live/route.py').read_bytes()).hexdigest()
    assert all('__pycache__' not in key for key in digests)


def test_the_identity_artefact_records_where_the_sources_came_from():
    identity = build_a13_macos.build_identity({'files': 1, 'sha256': {'a': 'b'}})
    assert identity['root'] == str(REPO)
    assert identity['head'] and len(identity['head']) == 40
    assert identity['branch']
    assert isinstance(identity['worktree_dirty'], bool)
    assert identity['compiled_not_shipped']
    # the shipped-source map must survive unchanged for everything that already reads it
    assert identity['files'] == 1 and identity['sha256'] == {'a': 'b'}
