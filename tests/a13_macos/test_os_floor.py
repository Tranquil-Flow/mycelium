# SPDX-License-Identifier: AGPL-3.0-only
"""macOS floor: friendly preflight refusal, Info.plist LSMinimumSystemVersion = Mach-O minos."""
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from mycelium_a13_macos.app import DemoError, preflight

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))


def test_preflight_refusal_names_detected_and_required_versions_and_what_to_do():
    with pytest.raises(DemoError) as caught:
        preflight(system='Darwin', machine='arm64', os_version='15.5')
    message = caught.value.user_message
    assert '15.5' in message and '26.0' in message
    assert 'Software Update' in message
    with pytest.raises(DemoError) as caught:
        preflight(system='Darwin', machine='x86_64', os_version='26.1')
    assert 'Apple Silicon' in caught.value.user_message and 'Intel' in caught.value.user_message


def test_launcher_prints_the_friendly_message_on_refusal(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
    code = ("import sys,platform;platform.mac_ver=lambda:('15.5',('','',''),'');"
            "sys.argv=['x','--preflight','--headless'];"
            "from mycelium_a13_macos.__main__ import main;raise SystemExit(main())")
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    refusal = json.loads(result.stderr.strip().splitlines()[-1])
    assert refusal['code'] == 'unsupported_platform'
    assert '26.0' in refusal['message'] and '15.5' in refusal['message']


def test_minos_parser_reads_a_real_system_binary():
    from build_a13_macos import macho_minimum_version
    version = macho_minimum_version(Path('/bin/ls'))
    assert version is not None and version.count('.') >= 1


def _fake_app(tmp_path, *binaries):
    """A bundle holding copies of /bin/ls re-stamped to the given minos values."""
    import shutil
    app = tmp_path / 'X.app'
    (app / 'Contents' / 'MacOS').mkdir(parents=True)
    with (app / 'Contents' / 'Info.plist').open('wb') as f:
        plistlib.dump({'CFBundleName': 'X'}, f)
    for index, minos in enumerate(binaries):
        target = app / 'Contents' / 'MacOS' / f'X{index}'
        if minos is None:
            shutil.copyfile('/bin/ls', target)
        else:
            subprocess.run(['/usr/bin/vtool', '-set-build-version', 'macos', minos, minos, '-replace',
                            '-output', str(target), '/bin/ls'], check=True, capture_output=True)
    return app


def _declared(app):
    with (app / 'Contents' / 'Info.plist').open('rb') as f:
        return plistlib.load(f)['LSMinimumSystemVersion']


def test_floor_is_the_macho_floor_when_it_is_below_the_lowest_admitted_macos(tmp_path):
    from build_a13_macos import set_bundle_minimum_system_version
    app = _fake_app(tmp_path, '11.0', '14.0')
    floor = set_bundle_minimum_system_version(app, admitted_versions={'15.6.1', '26.5.1'})
    assert floor['declared'] == '14.0' == _declared(app)
    assert floor['macho_minos_max'] == '14.0' and floor['admitted_floor'] == '15.6.1'
    assert floor['macho_minos_above_declared'] == []


def test_floor_never_exceeds_the_lowest_admitted_macos(tmp_path):
    # Homebrew-built runtimes are stamped minos 26.0 by their build host; the
    # product admits 15.6.1, so Finder must not refuse the app there.
    from build_a13_macos import set_bundle_minimum_system_version
    app = _fake_app(tmp_path, '11.0', '26.0')
    floor = set_bundle_minimum_system_version(app, admitted_versions={'15.6.1', '26.5.1'})
    assert floor['declared'] == '15.6.1' == _declared(app)
    assert floor['macho_minos_max'] == '26.0'
    assert floor['macho_minos_above_declared'] == ['Contents/MacOS/X1']


def test_floor_defaults_to_the_package_manifest_admitted_versions(tmp_path):
    from build_a13_macos import set_bundle_minimum_system_version
    from mycelium_a13_macos.package_manifest import ADMITTED_VERSIONS
    app = _fake_app(tmp_path, '26.0')
    floor = set_bundle_minimum_system_version(app)
    lowest = min(ADMITTED_VERSIONS, key=lambda v: tuple(int(p) for p in v.split('.')))
    assert floor['declared'] == lowest == _declared(app)


def test_floor_writer_refuses_a_bundle_without_macho(tmp_path):
    from build_a13_macos import set_bundle_minimum_system_version
    app = _fake_app(tmp_path)
    with pytest.raises(SystemExit, match='bundle_macho_floor_unknown'):
        set_bundle_minimum_system_version(app, admitted_versions={'15.6.1'})


def test_builder_does_not_hard_code_a_floor_above_the_admitted_versions():
    source = (ROOT / 'scripts' / 'build_a13_macos.py').read_text()
    assert "'26.0'" not in source


@pytest.mark.skipif(not os.environ.get('A13_PACKAGE_EXECUTABLE'), reason='needs the built package')
def test_built_bundle_declares_the_lowest_admitted_macos():
    from build_a13_macos import bundle_macho_floor
    from mycelium_a13_macos.package_manifest import ADMITTED_VERSIONS
    key = lambda v: tuple(int(p) for p in v.split('.'))
    app = Path(os.environ['A13_PACKAGE_EXECUTABLE']).parents[2]
    lowest_admitted = min(ADMITTED_VERSIONS, key=key)
    assert _declared(app) == min(bundle_macho_floor(app), lowest_admitted, key=key)
