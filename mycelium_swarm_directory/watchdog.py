# SPDX-License-Identifier: AGPL-3.0-only
"""Watchdog probe for the always-on directory unit.

    python -m mycelium_swarm_directory.watchdog --hosted --state-file <dir>/watchdog.json [--apply]

One run = one probe round (launchd ``StartInterval`` re-runs it).  It reads the directory's own
``/swarms/api/v1/config`` and page, and optionally the same through the apex proxy.  It counts
consecutive failures in a small state file; after ``--threshold`` of them it requests
``launchctl kickstart -k gui/<uid>/<label>`` -- printed only (dry run) unless ``--apply`` is given,
with a cooldown between restarts.  A failing proxy is reported but never restarts the directory
(the proxy belongs to the demo stack).  Prints one JSON line; exit 0 only when everything probed
is healthy.  Unknown (could not tell) counts as unhealthy.  It sends no data and writes only its
state file.
"""
from __future__ import annotations

from mycelium_http_identity import with_user_agent
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import hosted_defaults

_MAX_BODY = 65536


@dataclass(frozen=True)
class Result:
    ok: bool | None  # True healthy, False observed unhealthy, None could not be determined
    code: str


class DirectoryProbe:
    def __init__(self, base: str, expect_origin: str, *, client_ip: str | None = None,
                 opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = 5.0) -> None:
        self.base, self.expect_origin, self.client_ip = base.rstrip('/'), expect_origin, client_ip
        self.opener, self.timeout = opener, timeout

    def _get(self, path: str) -> bytes:
        headers = {'Accept': '*/*'}
        if self.client_ip:
            headers['Cf-Connecting-Ip'] = self.client_ip
        request = urllib.request.Request(self.base + path, headers=with_user_agent(headers), method='GET')
        with self.opener(request, timeout=self.timeout) as response:
            if getattr(response, 'status', 200) != 200:
                raise OSError('status')
            return response.read(_MAX_BODY)

    def run(self) -> Result:
        try:
            body = self._get(hosted_defaults.DIRECTORY_MOUNT_PREFIX + '/api/v1/config')
        except BaseException:  # noqa: BLE001 - a probe never raises
            return Result(None, 'transport_error')
        try:
            document = json.loads(body)
        except ValueError:
            return Result(None, 'invalid_json')
        if not isinstance(document, dict) or document.get('directory') != self.expect_origin:
            return Result(False, 'directory_origin_mismatch')
        try:
            page = self._get(hosted_defaults.DIRECTORY_MOUNT_PREFIX)
        except BaseException:  # noqa: BLE001
            return Result(False, 'page_unavailable')
        if b'<html' not in page.lower():
            return Result(False, 'page_invalid')
        return Result(True, 'ok')


class Watchdog:
    def __init__(self, directory: Any, proxy: Any, state_path: Path | str, restart_argv: list[str], *,
                 apply: bool = False, threshold: int = hosted_defaults.WATCHDOG_FAILURES_BEFORE_RESTART,
                 cooldown: float = 300.0, runner: Callable[..., Any] = subprocess.run,
                 clock: Callable[[], float] = time.time) -> None:
        self.directory, self.proxy, self.state_path = directory, proxy, Path(state_path)
        self.restart_argv, self.apply, self.threshold, self.cooldown = list(restart_argv), apply, threshold, cooldown
        self.runner, self.clock = runner, clock

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text())
            if isinstance(value, dict) and isinstance(value.get('failures'), int):
                return value
        except (OSError, ValueError):
            pass
        return {'failures': 0, 'last_restart': None}

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_path.with_suffix('.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(state, stream, sort_keys=True)
        os.replace(temporary, self.state_path)

    def step(self) -> dict[str, Any]:
        state, now = self._load(), self.clock()
        directory = self.directory.run()
        proxy = self.proxy.run() if self.proxy is not None else None
        action, command, exit_code = 'none', None, 0
        if directory.ok is True:
            state['failures'] = 0
        else:
            state['failures'] += 1
            if state['failures'] >= self.threshold:
                last = state.get('last_restart')
                if last is not None and now - last < self.cooldown:
                    action = 'cooldown'
                elif not self.apply:
                    action, command = 'would_restart', self.restart_argv
                else:
                    command = self.restart_argv
                    state['last_restart'] = now
                    try:
                        done = self.runner(self.restart_argv, capture_output=True, text=True, timeout=30)
                        restarted = done.returncode == 0
                    except BaseException:  # noqa: BLE001
                        restarted = False
                    action = 'restarted' if restarted else 'restart_failed'
                    if restarted:
                        state['failures'] = 0
        if directory.ok is not True or (proxy is not None and proxy.ok is not True):
            exit_code = 1
        self._save(state)
        return {'directory': {'ok': directory.ok, 'code': directory.code},
                'proxy': None if proxy is None else {'ok': proxy.ok, 'code': proxy.code},
                'failures': state['failures'], 'action': action, 'command': command, 'exit_code': exit_code}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m mycelium_swarm_directory.watchdog', allow_abbrev=False,
                                     description='Probe the directory unit; request a restart after repeated failures.')
    parser.add_argument('--hosted', action='store_true', help='probe the hosted-demo ports/origin (hosted_defaults.py)')
    parser.add_argument('--url', default=None, help='directory base URL, e.g. http://127.0.0.1:%d'
                        % hosted_defaults.DIRECTORY_PORT)
    parser.add_argument('--proxy-url', default=None, help='also probe through the apex proxy (never restarts)')
    parser.add_argument('--public-origin', default=hosted_defaults.DIRECTORY_PUBLIC_ORIGIN)
    parser.add_argument('--state-file', required=True)
    parser.add_argument('--threshold', type=int, default=hosted_defaults.WATCHDOG_FAILURES_BEFORE_RESTART)
    parser.add_argument('--cooldown', type=float, default=300.0)
    parser.add_argument('--label', default=hosted_defaults.DIRECTORY_LAUNCHD_LABEL)
    parser.add_argument('--uid', type=int, default=None)
    parser.add_argument('--apply', action='store_true', help='really run launchctl kickstart (default: print it)')
    return parser


def main(argv: list[str] | None = None, *, runner: Callable[..., Any] = subprocess.run,
         opener: Callable[..., Any] = urllib.request.urlopen, clock: Callable[[], float] = time.time) -> int:
    args = build_parser().parse_args(argv)
    url = args.url or ('http://127.0.0.1:%d' % hosted_defaults.DIRECTORY_PORT if args.hosted else None)
    proxy_url = args.proxy_url or ('http://127.0.0.1:%d' % hosted_defaults.PROXY_PORT if args.hosted else None)
    if url is None:
        print('--url or --hosted is required', file=sys.stderr)
        return 2
    uid = os.getuid() if args.uid is None else args.uid
    dog = Watchdog(DirectoryProbe(url, args.public_origin, opener=opener),
                   None if proxy_url is None else DirectoryProbe(proxy_url, args.public_origin,
                                                                 client_ip='127.0.0.1', opener=opener),
                   args.state_file, ['launchctl', 'kickstart', '-k', 'gui/%d/%s' % (uid, args.label)],
                   apply=args.apply, threshold=args.threshold, cooldown=args.cooldown, runner=runner, clock=clock)
    step = dog.step()
    print(json.dumps(step, sort_keys=True), flush=True)
    return step['exit_code']


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
