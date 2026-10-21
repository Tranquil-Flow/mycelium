# SPDX-License-Identifier: AGPL-3.0-only
"""The always-on directory unit (templates only) and its watchdog probe.

Nothing here installs or starts anything: templates are rendered to bytes, the start script is only
syntax-checked and dry-run, and the watchdog's restart is a printed command unless ``--apply`` is given.
FAKES (labelled): probes, the process runner and the clock in the watchdog state-machine tests.
"""
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from mycelium_swarm_directory import hosted_defaults as defaults
from mycelium_swarm_directory import server, unit, watchdog
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.crypto import verify
from mycelium_swarm_directory.store import MemoryStore

ROOT = Path(__file__).resolve().parents[2]
MAPPING = {name: '/tmp/x/' + name.lower() for name in unit.PLACEHOLDERS}


# -- templates ---------------------------------------------------------------------------------------

def test_directory_plist_template_renders_to_a_valid_always_on_unit():
    rendered = unit.render_unit('swarm-directory.plist', MAPPING)
    assert b'__' not in rendered
    plist = plistlib.loads(rendered)
    assert plist['Label'] == defaults.DIRECTORY_LAUNCHD_LABEL == 'now.mycelium.swarm-directory'
    assert plist['KeepAlive'] is True and plist['RunAtLoad'] is True and plist['Umask'] == 63
    assert plist['ProgramArguments'][0] == '/bin/bash'
    assert plist['ProgramArguments'][1].endswith('deploy/swarm-directory-start.sh')
    assert plist['WorkingDirectory'] == MAPPING['MYCELIUM_REPO_ROOT']
    env = plist['EnvironmentVariables']
    assert env['MYCELIUM_SWARM_DIR_STATE_DIR'] == MAPPING['SWARM_DIR_STATE_DIR']
    assert env['MYCELIUM_PYTHON'] == MAPPING['MYCELIUM_PYTHON']
    assert not any('TOKEN' in k or 'SECRET' in k or 'KEY' in k for k in env)


def test_watchdog_plist_template_runs_the_probe_on_an_interval_and_never_applies_by_default():
    plist = plistlib.loads(unit.render_unit('swarm-directory-watchdog.plist', MAPPING))
    assert plist['Label'] == defaults.DIRECTORY_WATCHDOG_LAUNCHD_LABEL
    assert plist['StartInterval'] == defaults.WATCHDOG_INTERVAL_SECONDS
    assert 'KeepAlive' not in plist or plist['KeepAlive'] is False
    args = plist['ProgramArguments']
    assert args[-1] != '--apply' and '-m' in args and 'mycelium_swarm_directory.watchdog' in args
    assert '--state-file' in args


def test_render_refuses_missing_placeholders_and_unknown_templates():
    with pytest.raises(unit.UnitError, match='unsatisfied_placeholder'):
        unit.render_unit('swarm-directory.plist', {k: v for k, v in MAPPING.items() if k != 'MYCELIUM_PYTHON'})
    with pytest.raises(unit.UnitError, match='template_unknown'):
        unit.render_unit('../demo-watchdog.plist', MAPPING)


@pytest.mark.skipif(shutil.which('plutil') is None, reason='plutil is macOS only')
def test_rendered_plists_pass_plutil_lint(tmp_path):
    for name in ('swarm-directory.plist', 'swarm-directory-watchdog.plist'):
        target = tmp_path / name
        target.write_bytes(unit.render_unit(name, MAPPING))
        done = subprocess.run(['plutil', '-lint', str(target)], capture_output=True, text=True, timeout=20)
        assert done.returncode == 0, done.stdout + done.stderr


def test_raw_templates_are_marked_as_templates_and_never_install_anything():
    for name in ('swarm-directory.plist', 'swarm-directory-watchdog.plist', 'swarm-directory-start.sh'):
        text = (ROOT / 'deploy' / name).read_text()
        assert 'TEMPLATE' in text and 'not installed' in text.lower()
    script = (ROOT / 'deploy' / 'swarm-directory-start.sh').read_text()
    assert 'launchctl' not in script and 'cloudflared' not in script and 'ssh ' not in script


# -- start script ---------------------------------------------------------------------------------------

def test_start_script_is_valid_bash_and_dry_run_has_no_side_effects(tmp_path):
    script = ROOT / 'deploy' / 'swarm-directory-start.sh'
    assert subprocess.run(['bash', '-n', str(script)], capture_output=True, timeout=20).returncode == 0
    state = tmp_path / 'state'
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'MYCELIUM_SWARM_DIR_STATE_DIR': str(state),
           'MYCELIUM_PYTHON': '/usr/bin/python3', 'MYCELIUM_SWARM_DIR_DRY_RUN': '1'}
    done = subprocess.run(['bash', str(script)], capture_output=True, text=True, timeout=20, env=env, cwd=ROOT)
    assert done.returncode == 0, done.stderr
    assert not state.exists()  # dry run creates nothing
    line = done.stdout.strip().splitlines()[-1]
    assert '-m mycelium_swarm_directory --hosted --db %s/directory.sqlite3' % state in line
    assert 'launchctl' not in done.stdout
    refused = subprocess.run(['bash', str(script)], capture_output=True, text=True, timeout=20, cwd=ROOT,
                             env={k: v for k, v in env.items() if k != 'MYCELIUM_SWARM_DIR_STATE_DIR'})
    assert refused.returncode != 0 and 'MYCELIUM_SWARM_DIR_STATE_DIR' in refused.stderr


def test_hosted_flag_applies_the_named_defaults_and_stays_overridable():
    parser = server.build_parser()
    hosted = server.resolve(parser.parse_args(['--db', 'x', '--hosted']))
    assert (hosted.port, hosted.public_origin, hosted.trusted_proxy) == (4360, 'https://mycelium.now', True)
    other = server.resolve(parser.parse_args(['--db', 'x', '--hosted', '--port', '4999',
                                              '--public-origin', 'https://swarms.example.org']))
    assert (other.port, other.public_origin, other.trusted_proxy) == (4999, 'https://swarms.example.org', True)
    plain = server.resolve(parser.parse_args(['--db', 'x']))
    assert (plain.port, plain.public_origin, plain.trusted_proxy) == (4360, None, False)


# -- watchdog probe -----------------------------------------------------------------------------------------

class Opener:
    """FAKE urllib opener serving canned (status, body) per URL suffix; records request headers."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, dict(request.header_items())))
        for suffix, answer in self.routes.items():
            if request.full_url.endswith(suffix):
                if isinstance(answer, BaseException):
                    raise answer
                status, body = answer
                return io.BytesIO(body) if status == 200 else (_ for _ in ()).throw(OSError('http %d' % status))
        raise OSError('no route')


GOOD = json.dumps({'directory': 'https://mycelium.now', 'privacy': 'p', 'demo': 'd', 'limits': {}}).encode()


def test_probe_is_green_only_for_the_expected_config_document():
    probe = watchdog.DirectoryProbe('http://127.0.0.1:4360', 'https://mycelium.now',
                                    opener=Opener({'/swarms/api/v1/config': (200, GOOD), '/swarms': (200, b'<html>')}))
    assert probe.run() == watchdog.Result(True, 'ok')
    wrong = json.dumps({'directory': 'http://127.0.0.1:4360'}).encode()
    for routes, want in (({'/swarms/api/v1/config': (200, wrong), '/swarms': (200, b'<html>')}, False),
                         ({'/swarms/api/v1/config': (200, b'nope'), '/swarms': (200, b'<html>')}, None),
                         ({'/swarms/api/v1/config': (200, GOOD)}, False),  # page missing
                         ({}, None)):
        result = watchdog.DirectoryProbe('http://127.0.0.1:4360', 'https://mycelium.now', opener=Opener(routes)).run()
        assert result.ok is want, (routes, result)


def test_probe_through_the_apex_proxy_sends_a_client_address():
    opener = Opener({'/swarms/api/v1/config': (200, GOOD), '/swarms': (200, b'<html>')})
    watchdog.DirectoryProbe('http://127.0.0.1:4350', 'https://mycelium.now', client_ip='127.0.0.1',
                            opener=opener).run()
    assert all(headers.get('Cf-connecting-ip') == '127.0.0.1' for _, headers in opener.calls)


class FakeProbe:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    def run(self):
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]


class Runner:
    def __init__(self, returncode=0):
        self.calls, self.returncode = [], returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, '', '')


BAD, GREEN, UNKNOWN = watchdog.Result(False, 'down'), watchdog.Result(True, 'ok'), watchdog.Result(None, 'timeout')


def make(tmp_path, directory, proxy=None, *, apply=False, runner=None, clock=None, **kw):
    runner = runner or Runner()
    return watchdog.Watchdog(directory, proxy, tmp_path / 'wd.json', ['launchctl', 'kickstart', '-k', 'gui/501/x'],
                             apply=apply, runner=runner, clock=clock or (lambda: 1000.0), **kw), runner


def test_healthy_probe_resets_the_failure_count_and_never_restarts(tmp_path):
    dog, runner = make(tmp_path, FakeProbe(BAD, BAD, GREEN, BAD), apply=True)
    assert [dog.step()['failures'] for _ in range(4)] == [1, 2, 0, 1]
    assert runner.calls == []


def test_restart_is_only_printed_in_dry_run_and_runs_with_apply(tmp_path):
    dog, runner = make(tmp_path, FakeProbe(BAD))
    steps = [dog.step() for _ in range(3)]
    assert [s['action'] for s in steps] == ['none', 'none', 'would_restart']
    assert steps[2]['command'] == ['launchctl', 'kickstart', '-k', 'gui/501/x'] and runner.calls == []
    (tmp_path / 'b').mkdir()
    dog, runner = make(tmp_path / 'b', FakeProbe(BAD), apply=True)
    assert [dog.step()['action'] for _ in range(3)] == ['none', 'none', 'restarted']
    assert runner.calls == [['launchctl', 'kickstart', '-k', 'gui/501/x']]


def test_unknown_counts_as_unhealthy_and_a_restart_cooldown_stops_loops(tmp_path):
    now = {'t': 1000.0}
    dog, runner = make(tmp_path, FakeProbe(UNKNOWN), apply=True, clock=lambda: now['t'], threshold=2, cooldown=300)
    assert [dog.step()['action'] for _ in range(2)] == ['none', 'restarted']
    assert [dog.step()['action'] for _ in range(3)] == ['none', 'cooldown', 'cooldown']
    now['t'] += 301
    assert dog.step()['action'] == 'restarted' and len(runner.calls) == 2


def test_failed_restart_command_is_reported_and_state_is_kept(tmp_path):
    dog, runner = make(tmp_path, FakeProbe(BAD), apply=True, runner=Runner(returncode=113), threshold=1)
    step = dog.step()
    assert step['action'] == 'restart_failed' and step['exit_code'] == 1
    assert stat.S_IMODE(os.stat(tmp_path / 'wd.json').st_mode) == 0o600


def test_a_down_proxy_is_reported_but_never_restarts_the_directory(tmp_path):
    dog, runner = make(tmp_path, FakeProbe(GREEN), FakeProbe(BAD), apply=True, threshold=1)
    step = dog.step()
    assert step['action'] == 'none' and step['proxy']['code'] == 'down' and step['failures'] == 0
    assert step['exit_code'] == 1 and runner.calls == []


def test_corrupt_state_file_is_treated_as_empty(tmp_path):
    (tmp_path / 'wd.json').write_text('{not json')
    dog, _ = make(tmp_path, FakeProbe(BAD))
    assert dog.step()['failures'] == 1


def test_cli_dry_run_against_a_real_directory_server_is_green_and_writes_only_its_state_file(tmp_path, capsys):
    served = server.DirectoryServer(Directory(MemoryStore(), verify), port=0,
                                    public_origin='https://mycelium.now', trusted_proxy=True).start()
    try:
        state = tmp_path / 'state' / 'wd.json'
        code = watchdog.main(['--url', served.origin, '--state-file', str(state)])
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert code == 0 and out['directory'] == {'ok': True, 'code': 'ok'} and out['action'] == 'none'
        code = watchdog.main(['--url', served.origin, '--public-origin', 'https://other.example.org',
                              '--state-file', str(state)])
        assert code == 1
        assert sorted(p.name for p in state.parent.iterdir()) == ['wd.json']
    finally:
        served.close()


def test_cli_apply_is_the_only_way_to_run_the_restart(tmp_path, capsys):
    runner = Runner()
    args = ['--url', 'http://127.0.0.1:1', '--state-file', str(tmp_path / 'wd.json'), '--threshold', '1',
            '--label', defaults.DIRECTORY_LAUNCHD_LABEL, '--uid', '501']
    assert watchdog.main(args, runner=runner) == 1 and runner.calls == []
    assert 'would_restart' in capsys.readouterr().out
    assert watchdog.main([*args, '--apply'], runner=runner) == 1
    assert runner.calls == [['launchctl', 'kickstart', '-k', 'gui/501/' + defaults.DIRECTORY_LAUNCHD_LABEL]]
