# SPDX-License-Identifier: AGPL-3.0-only
"""Native process exit must leave a bounded, privacy-reduced diagnostic.

Regression: the serving native worker exited, but nothing recorded *why* — so
preview.31's ``infer_start`` failure and preview.32's ``hello`` failure were
both causeless. The session keeps only a bounded stderr tail; this records the
derived frames/exception classes, never raw stderr, source lines or messages.
No model execution here.
"""
import json
import stat
import threading
from types import SimpleNamespace as NS

from mycelium_a13_macos.worker import ManagedWorker

STDERR = (
    b'Traceback (most recent call last):\n'
    b'  File "/private/location/physical_inference_node.py", line 2617, in _infer_start\n'
    b'    SECRET_CANARY_SOURCE\n'
    b'  File "/private/location/mycelium_router/transports/iroh.py", line 88, in send\n'
    b'IrohTransportError: SECRET_CANARY_MESSAGE\n'
)


class _GuardStub:
    """Real guard method (bound via the class) over fake worker state."""

    _record_native_exit = ManagedWorker._record_native_exit

    def __init__(self, **state):
        self.__dict__.update(state)


def test_process_exit_records_frames_and_never_raw_text(tmp_path):
    session = NS(returncode=1, stderr=STDERR)
    worker = NS(
        capacity_root=tmp_path,
        run_id='fixture-run',
        node_id='fixture-node',
        _error='worker_native_exited',
    )
    ManagedWorker._record_native_exit(worker, session)
    path = tmp_path / 'native-exit.private.json'
    document = json.loads(path.read_text())
    assert document['protocol'] == 'mycelium.a13.native_exit_diagnostic.v1'
    assert document['run_id'] == 'fixture-run'
    assert document['node_id'] == 'fixture-node'
    assert document['returncode'] == 1
    # A real process exit and a guard trip must not read the same.
    assert document['error_code'] == 'worker_native_exited'
    assert document['frames'] == [
        {'file': 'physical_inference_node.py', 'line': 2617, 'function': '_infer_start'},
        {'file': 'iroh.py', 'line': 88, 'function': 'send'},
    ]
    assert document['exception_types'] == ['IrohTransportError']
    text = path.read_text()
    assert 'SECRET_CANARY' not in text
    assert '/private/location' not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_process_exit_diagnostic_is_skipped_without_a_capacity_root():
    ManagedWorker._record_native_exit(
        NS(capacity_root=None), NS(returncode=None, stderr=b'')
    )


def test_watch_records_the_exit_then_closes_the_worker(tmp_path):
    closed = threading.Event()
    calls = []

    def close():
        calls.append('close')
        closed.set()

    worker = _GuardStub(
        capacity_root=tmp_path,
        run_id='fixture-run',
        node_id='fixture-node',
        _closed=closed,
        _session=NS(returncode=9, stderr=STDERR),
        _error=None,
        _check=lambda *args, **kwargs: None,
        close=close,
    )
    ManagedWorker._watch(worker)
    assert calls == ['close']
    assert worker._error == 'worker_native_exited'
    document = json.loads((tmp_path / 'native-exit.private.json').read_text())
    assert document['returncode'] == 9
    assert document['error_code'] == 'worker_native_exited'
    assert document['exception_types'] == ['IrohTransportError']


def test_exit_diagnostic_names_the_command_the_native_could_not_answer(tmp_path):
    """A silent clean exit is only actionable if the record says what it cost.

    Measured 2026-09-19 (round p76r1, preview.76): the peer's native exited with
    ``returncode`` 0 and an EMPTY stderr, so every command the peer then claimed
    was answered ``reconciliation_required`` within 70 ms of the claim, and the
    owner -- correctly refusing an unaccounted command -- failed the deployment
    at startup qualification.  Nothing in this record connected the exit to a
    command, so the cost had to be reconstructed from timestamps.
    """

    worker = NS(
        capacity_root=tmp_path,
        run_id='fixture-run',
        node_id='fixture-node',
        _error='worker_native_exited',
        _pending_commands={'cmd-1111222233334444', 'cmd-aaaabbbbccccdddd'},
    )
    ManagedWorker._record_native_exit(worker, NS(returncode=0, stderr=b''))
    document = json.loads((tmp_path / 'native-exit.private.json').read_text())
    # A clean exit with no stderr is exactly the case that stayed unexplained.
    assert document['returncode'] == 0
    assert document['exception_types'] == [] and document['frames'] == []
    # ... and now it says what it cost.
    assert document['in_flight_count'] == 2
    assert sorted(document['in_flight_commands']) == [
        'cmd-1111222233334444',
        'cmd-aaaabbbbccccdddd',
    ]


def test_exit_diagnostic_says_zero_when_nothing_was_in_flight(tmp_path):
    worker = NS(
        capacity_root=tmp_path,
        run_id='fixture-run',
        node_id='fixture-node',
        _error='worker_native_exited',
        _pending_commands=set(),
    )
    ManagedWorker._record_native_exit(worker, NS(returncode=0, stderr=b''))
    document = json.loads((tmp_path / 'native-exit.private.json').read_text())
    # An idle exit must not read like an interrupted command.
    assert document['in_flight_count'] == 0
    assert document['in_flight_commands'] == []


def test_exit_diagnostic_bounds_the_in_flight_names_but_not_the_count(tmp_path):
    """Bounded like every other record: the names are capped, the count is honest."""

    pending = {'cmd-%032x' % index for index in range(9)}
    worker = NS(
        capacity_root=tmp_path,
        run_id='fixture-run',
        node_id='fixture-node',
        _error='worker_native_exited',
        _pending_commands=pending,
    )
    ManagedWorker._record_native_exit(worker, NS(returncode=0, stderr=b''))
    document = json.loads((tmp_path / 'native-exit.private.json').read_text())
    assert document['in_flight_count'] == len(pending)
    assert len(document['in_flight_commands']) <= 3


def test_exit_diagnostic_tolerates_a_worker_without_the_reservation_set(tmp_path):
    """The record must never be the thing that fails: absence means zero, not a crash."""

    worker = NS(
        capacity_root=tmp_path,
        run_id='fixture-run',
        node_id='fixture-node',
        _error='worker_native_exited',
    )
    ManagedWorker._record_native_exit(worker, NS(returncode=0, stderr=b''))
    document = json.loads((tmp_path / 'native-exit.private.json').read_text())
    assert document['in_flight_count'] == 0
    assert document['in_flight_commands'] == []
