# SPDX-License-Identifier: AGPL-3.0-only
"""The peer's reason for an honest "unknown" must survive the ack that retires the claim.

Measured 2026-09-19 (round p76r1, preview.76): the peer claimed ``configure``, could not produce a
native response, and acked ``disposition='reconciliation_required'`` -- honest and correct.  The ack
protocol carries no reason field (``_ACK_FIELDS`` is command_id, claim_token, disposition,
native_response), and the claim row is then popped -- ``error_code`` included -- on the stated ground
that the ack is durable owner-side.  So the owner learned only "unknown", the peer kept nothing, and
the round could not name its own cause: it had to be reconstructed from a cross-machine timestamp
triangle.  These tests pin a bounded LOCAL record of that reason.

Bounded and privacy-reduced like every other private record here: a truncated command id, one reason
code, a count.  No payloads, no claim tokens, no key material, no raw stderr.
"""
import json
import stat
from types import SimpleNamespace as NS

from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump


def _pump(root, **state):
    """Real method (bound via the class) over production-shaped fake state.

    ``worker`` is provided exactly as the pump gets it, because reading it the wrong way was a
    silent no-op the first time this record was written: the diagnostic wrote nothing and said
    nothing, which is the failure mode the record exists to end.
    """

    class _Recorder:
        _record_uncertain_ack = ConcurrentTargetControlPump._record_uncertain_ack

        def __init__(self, **fields):
            self.root = root
            self.__dict__.update(fields)

    return _Recorder(**state)


def test_uncertain_ack_keeps_the_reason_the_ack_cannot_carry(tmp_path):
    pump = _pump(tmp_path, worker=NS(run_id='fixture-run'))
    ConcurrentTargetControlPump._record_uncertain_ack(
        pump, 'cmd-aaaabbbbccccdddd1234', 'worker_command_reconciliation_required', 2
    )
    path = tmp_path / 'uncertain-ack.private.json'
    document = json.loads(path.read_text())
    assert document['protocol'] == 'mycelium.a13.uncertain_ack.v1'
    assert document['run_id'] == 'fixture-run'
    assert document['error_code'] == 'worker_command_reconciliation_required'
    assert document['remaining_claims'] == 2
    # The command id is truncated, and nothing else about the command travels.
    assert document['command_id_prefix'] == 'cmd-aaaabbbbcccc'
    assert 'cmd-aaaabbbbccccdddd1234' not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_uncertain_ack_records_an_absent_reason_honestly(tmp_path):
    """No reason known must read as no reason known -- never as a fabricated code."""

    pump = _pump(tmp_path, worker=NS(run_id='fixture-run'))
    ConcurrentTargetControlPump._record_uncertain_ack(pump, 'cmd-1111222233334444', None, 0)
    document = json.loads((tmp_path / 'uncertain-ack.private.json').read_text())
    assert document['error_code'] is None
    assert document['remaining_claims'] == 0


def test_uncertain_ack_still_records_without_a_worker_handle(tmp_path):
    """A missing handle degrades the run id, it does not silently skip the record.

    This is the regression for the first version of this method: it read ``self.worker.run_id``
    directly inside a swallow-everything guard, so a wrong read meant no file and no complaint.
    """

    pump = _pump(tmp_path)
    ConcurrentTargetControlPump._record_uncertain_ack(
        pump, 'cmd-aaaabbbbccccdddd', 'worker_native_exited', 1
    )
    path = tmp_path / 'uncertain-ack.private.json'
    assert path.exists(), 'the record must exist even when the handle is absent'
    document = json.loads(path.read_text())
    assert document['run_id'] is None
    assert document['error_code'] == 'worker_native_exited'


def test_uncertain_ack_can_never_be_the_thing_that_fails(tmp_path):
    """A diagnostic must not raise, even when its own record cannot be written."""

    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('this path is a file, not a root')
    pump = _pump(blocked, worker=NS(run_id='fixture-run'))
    ConcurrentTargetControlPump._record_uncertain_ack(
        pump, 'cmd-aaaabbbbccccdddd', 'worker_native_exited', 1
    )
