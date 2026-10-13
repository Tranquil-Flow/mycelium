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


def test_floor_writer_sets_the_plist_key_and_refuses_a_lower_floor(tmp_path):
    from build_a13_macos import set_bundle_minimum_system_version
    app = tmp_path / 'X.app'
    (app / 'Contents' / 'MacOS').mkdir(parents=True)
    with (app / 'Contents' / 'Info.plist').open('wb') as f:
        plistlib.dump({'CFBundleName': 'X'}, f)
    import shutil
    shutil.copyfile('/bin/ls', app / 'Contents' / 'MacOS' / 'X')
    floor = set_bundle_minimum_system_version(app, minimum=None)
    with (app / 'Contents' / 'Info.plist').open('rb') as f:
        assert plistlib.load(f)['LSMinimumSystemVersion'] == floor
    with pytest.raises(SystemExit, match='bundle_floor_below_macho_minos'):
        set_bundle_minimum_system_version(app, minimum='10.0')


@pytest.mark.skipif(not os.environ.get('A13_PACKAGE_EXECUTABLE'), reason='needs the built package')
def test_built_bundle_declares_26_0_and_it_matches_the_macho_minos():
    from build_a13_macos import bundle_macho_floor
    app = Path(os.environ['A13_PACKAGE_EXECUTABLE']).parents[2]
    with (app / 'Contents' / 'Info.plist').open('rb') as f:
        declared = plistlib.load(f)['LSMinimumSystemVersion']
    assert declared == '26.0'
    assert bundle_macho_floor(app) == '26.0'
