# SPDX-License-Identifier: AGPL-3.0-only
"""The packaged ``--node-process-launcher`` entry must not import the app.

``PhysicalNodeProcess`` runs ``<app exe> --node-process-launcher`` in a frozen
app, and the launcher's ready byte is inside the 2.0 s launch budget.  The
entry module used to import the whole demo app (``DemoApp`` and everything
behind it) at module level and then ``mycelium_node.process`` (the node
package) before it could signal ready -- 0.22-0.43 s idle, and unbounded on a
joined 8 GB Mac deep in swap right after staging.  The trampoline is now
dispatched before any of that is imported.
"""

import os
from pathlib import Path
import select
import subprocess
import sys

import pytest

import mycelium_node.process as process_module

REPO = Path(__file__).resolve().parents[2]
ENTRY = REPO / 'mycelium_a13_macos' / '__main__.py'
_RUN_AS_FROZEN_ENTRY = (
    "import runpy,sys;sys.path.insert(0,%r);sys.argv=[%r,*sys.argv[1:]];"
    "runpy.run_path(%r,run_name='__main__')" % (str(REPO), str(ENTRY), str(ENTRY)))


def test_the_entry_trampoline_is_byte_identical_to_the_node_launcher_source():
    from mycelium_a13_macos import __main__ as app_main

    assert app_main._NODE_PROCESS_LAUNCHER_SOURCE == process_module._LAUNCHER_SOURCE


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX launcher handshake')
def test_launcher_entry_signals_ready_and_execs_without_importing_the_app(tmp_path):
    marker = tmp_path / 'exec-marker'
    ready_r, ready_w = os.pipe()
    release_r, release_w = os.pipe()
    exec_r, exec_w = os.pipe()
    target = [sys.executable, '-I', '-S', '-c',
              'import sys; open(sys.argv[1], "w").write("exec")', str(marker)]
    process = subprocess.Popen(
        [sys.executable, '-B', '-X', 'importtime', '-c', _RUN_AS_FROZEN_ENTRY,
         '--node-process-launcher', str(ready_w), str(release_r), str(exec_w), *target],
        pass_fds=(ready_w, release_r, exec_w), cwd='/', stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE)
    try:
        for descriptor in (ready_w, release_r, exec_w):
            os.close(descriptor)
        assert select.select([ready_r], [], [], 30)[0], 'launcher never signalled ready'
        assert os.read(ready_r, 1) == b'R'
        os.write(release_w, b'G')
        assert select.select([exec_r], [], [], 30)[0]
        assert os.read(exec_r, 1) == b''  # close-on-exec EOF: the exec happened
        _, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for descriptor in (ready_r, release_w, exec_r):
            os.close(descriptor)
    assert process.returncode == 0
    assert marker.read_text() == 'exec'
    imported = [line.split('|')[-1].strip() for line in stderr.decode().splitlines()
                if line.startswith('import time:')][1:]
    heavy = [name for name in imported
             if name.startswith(('mycelium', 'scripts', 'argparse', 'webbrowser', 'threading'))]
    assert heavy == []


def test_launcher_entry_through_main_still_dispatches(monkeypatch):
    """``main()`` keeps the branch: exec of the trampoline, never app code."""

    from mycelium_a13_macos import __main__ as app_main

    seen = []
    monkeypatch.setattr(app_main, '_run_node_process_launcher', lambda argv: seen.append(argv) or 126)
    monkeypatch.setattr(sys, 'argv', ['Mycelium A13 Demo', '--node-process-launcher', '5', '6', '7', '/x'])
    assert app_main.main() == 126
    assert seen == [['Mycelium A13 Demo', '--node-process-launcher', '5', '6', '7', '/x']]
