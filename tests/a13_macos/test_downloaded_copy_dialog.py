# SPDX-License-Identifier: AGPL-3.0-only
"""Dialog selection only: fake files, no app or OS dialog is executed."""
from pathlib import Path
import sys
import pytest
from mycelium_a13_macos import launch_location
from mycelium_a13_macos import __main__ as entry
from tests.a13_macos.test_launch_location import EXE_TAIL, TRANSLOCATED

MESSAGE = ('This is the downloaded copy. Mycelium is installed: open Mycelium A13 '
           'from the Applications folder in your home folder.')


@pytest.mark.parametrize('location', ['downloads', 'translocated'])
@pytest.mark.parametrize('installed', ['valid', 'missing', 'broken', 'unrelated'])
def test_dialog_checks_real_installed_entry(tmp_path, installed, location):
    programs = launch_location.programs_root(tmp_path)
    app_link = tmp_path / 'Applications' / 'Mycelium A13.app'
    app_link.parent.mkdir()
    target = programs / '0.1.4' / EXE_TAIL
    if installed == 'unrelated':
        target = tmp_path / 'Downloads' / EXE_TAIL
    if installed in {'valid', 'unrelated'}:
        target.parent.mkdir(parents=True)
        target.write_bytes(b'fixture, never executable')
    if installed != 'missing':
        app_link.symlink_to(target.parents[2])
    downloaded = tmp_path / 'Downloads' / 'new' / EXE_TAIL
    refusal = launch_location.not_installed_refusal(frozen=True, platform='darwin', argv=['app'],
        executable=TRANSLOCATED if location == 'translocated' else downloaded,
        programs_root=programs, applications_entry=app_link)
    assert refusal['message'] == (MESSAGE if installed == 'valid' else launch_location.MESSAGE)


def test_main_uses_downloaded_copy_dialog_without_starting_app(tmp_path, monkeypatch):
    programs = launch_location.programs_root(tmp_path)
    exe = programs / '0.1.4' / EXE_TAIL
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b'data only')
    link = tmp_path / 'Applications' / 'Mycelium A13.app'
    link.parent.mkdir()
    link.symlink_to(exe.parents[2])
    shown = []
    def forbidden(*args, **kwargs):
        raise AssertionError('must not start a session')
    monkeypatch.setattr(entry.Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(sys, 'argv', ['app'])
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'Downloads' / EXE_TAIL))
    monkeypatch.setattr(entry, 'DemoApp', forbidden)
    monkeypatch.setattr(entry, 'preflight', forbidden)
    monkeypatch.setattr(entry, '_show_refusal_dialog', shown.append)
    assert entry.main() == 2
    assert shown == [MESSAGE]
