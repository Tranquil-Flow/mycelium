# SPDX-License-Identifier: AGPL-3.0-only
"""A budget-exhausted reconnect must leave evidence of HOW LONG it tried.

Measured 2026-09-18: the soak01 round exhausted the 180 s reconnect budget on
`local_api_http_409:seed_http_remote_error` (the laptop could not reach the seed origin), and the attempt
observations were lost -- they were written only on the success path. So the failure that decides whether
180 s is the right budget left no record of the duration. A failure that cannot be measured cannot size its
own budget.
"""
import ast
import json
import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = (REPO / 'scripts/a13_operator/round14_bootstrap_compile.py').read_text()


def test_the_attempt_dump_exists_and_is_called_before_the_raise():
    """The wiring is the point: a helper nothing calls would look like a fix without being one."""
    tree = ast.parse(SRC)
    assert any(isinstance(n, ast.FunctionDef) and n.name == '_dump_failed_attempts'
               for n in ast.walk(tree)), 'helper missing'
    i = SRC.index('if not is_retryable(')
    window = SRC[i:i + 400]
    assert '_dump_failed_attempts(observations)' in window, 'not called at the exhaustion site'
    assert window.index('_dump_failed_attempts') < window.index('raise'), 'must dump BEFORE raising'


def test_the_helper_writes_the_attempts_it_was_given(tmp_path, monkeypatch):
    """Run the helper for real (it touches no network) and read back what it wrote."""
    ns = {}
    src = SRC
    # extract just the helper so the module's ssh/rpc imports are not needed
    start = src.index('def _dump_failed_attempts')
    end = src.index('def reconnect_target')
    body = src[start:end]
    consts = 'RECONNECT_BUDGET_SECONDS=180.0\n'
    exec(compile('import json, os\n' + consts + body, '<helper>', 'exec'), ns)
    monkeypatch.setenv('A13_EVIDENCE_DIR', str(tmp_path))
    observations = [{'attempt': 1, 'outcome': 'failed', 'transient': True,
                     'codes': ['seed_http_remote_error'], 'elapsed_seconds': 12.5}]
    ns['_dump_failed_attempts'](observations)
    written = json.loads((tmp_path / 'reconnect-attempts.json').read_text())
    assert written['protocol'] == 'mycelium.a13.reconnect_attempts.v1'
    assert written['reconnect_budget_seconds'] == 180.0
    assert written['attempts'] == observations


def test_a_failed_dump_never_masks_the_original_error(tmp_path, monkeypatch):
    """Evidence collection must not become a new failure mode."""
    ns = {}
    src = SRC
    start = src.index('def _dump_failed_attempts')
    end = src.index('def reconnect_target')
    exec(compile('import json, os\nRECONNECT_BUDGET_SECONDS=180.0\n' + src[start:end], '<h>', 'exec'), ns)
    monkeypatch.setenv('A13_EVIDENCE_DIR', '/definitely/not/writable/dir')
    ns['_dump_failed_attempts']([{'attempt': 1}])   # must not raise
