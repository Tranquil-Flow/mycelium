# SPDX-License-Identifier: AGPL-3.0-only
"""wp8-03 item 3: retained-state resume must be PROVEN, not assumed (the ERRATA-1 lesson).

`runs/wp8-02-useful-two-mac/ERRATA-1.md`: a partial or silently re-fetched stage could reach the
state NAME `prepared` while the total was not present, and the round then proceeded on an incomplete
model -- a failure that cost a physical attempt and presented as something else entirely.

The state name alone is therefore not evidence. These tests pin what the assertion demands, and --
just as important -- that every failure names ITSELF with the numbers, so the next occurrence is one
read off disk instead of another investigation.
"""
import importlib.util
import os
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
OPERATOR = ROOT / 'scripts/a13_operator'

# The module imports round14_rpc, which demands four A13_* env vars at import time.
_owner = pathlib.Path(tempfile.mkdtemp()) / 'owner-lhr'
_owner.mkdir(parents=True, exist_ok=True)
os.environ.setdefault('A13_OWNER_ROOT', str(_owner))
os.environ.setdefault('A13_REMOTE_ROOT', '/Users/example/remote')
os.environ.setdefault('A13_TARGET_PREVIEW', 'preview70')
os.environ.setdefault('A13_RPC_ROUND', 'rpc-round70')
if str(OPERATOR) not in sys.path:
    sys.path.insert(0, str(OPERATOR))

_spec = importlib.util.spec_from_file_location(
    'round14_bootstrap_compile_probe', OPERATOR / 'round14_bootstrap_compile.py')
bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bootstrap)


def _status(**acquisition):
    return {'acquisition': acquisition}


def test_a_fully_retained_acquisition_passes():
    record = bootstrap.assert_retained_acquisition(
        _status(state='prepared', downloaded_bytes=15_231_271_888, total_bytes=15_231_271_888),
        where='resume:owner')
    assert record['state'] == 'prepared'


def test_a_prepared_node_with_a_short_total_fails_with_the_numbers():
    """The ERRATA-1 shape: the state name is right and the bytes are not."""
    with pytest.raises(RuntimeError) as caught:
        bootstrap.assert_retained_acquisition(
            _status(state='prepared', downloaded_bytes=7_810_167_792, total_bytes=15_231_271_888),
            where='resume:target')
    message = str(caught.value)
    assert message.startswith('retained_acquisition_incomplete:resume:target:')
    assert '7810167792' in message and '15231271888' in message


def test_a_node_that_is_not_prepared_is_named_as_such():
    with pytest.raises(RuntimeError, match='retained_acquisition_not_prepared:resume:owner:starting'):
        bootstrap.assert_retained_acquisition(_status(state='starting', downloaded_bytes=0,
                                                     total_bytes=0), where='resume:owner')


def test_a_zero_or_missing_total_fails_rather_than_passing_a_weak_check():
    """`total <= 0` must never read as 'nothing missing'."""
    for acquisition in ({'state': 'prepared', 'downloaded_bytes': 0, 'total_bytes': 0},
                        {'state': 'prepared', 'downloaded_bytes': 1, 'total_bytes': None},
                        {'state': 'prepared'}):
        with pytest.raises(RuntimeError, match='retained_acquisition_unmeasured:resume:owner'):
            bootstrap.assert_retained_acquisition(_status(**acquisition), where='resume:owner')


def test_a_boolean_is_not_accepted_as_a_byte_count():
    """True == 1 in Python; a bool must not slip through as a measurement."""
    with pytest.raises(RuntimeError, match='retained_acquisition_unmeasured:resume:owner'):
        bootstrap.assert_retained_acquisition(
            _status(state='prepared', downloaded_bytes=True, total_bytes=10), where='resume:owner')


def test_an_empty_status_is_not_crashed_on():
    with pytest.raises(RuntimeError, match='retained_acquisition_not_prepared:resume:owner:None'):
        bootstrap.assert_retained_acquisition({}, where='resume:owner')
