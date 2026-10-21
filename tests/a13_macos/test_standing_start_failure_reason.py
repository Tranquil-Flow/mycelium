# SPDX-License-Identifier: AGPL-3.0-only
"""The joined member's participant detail names the failed launch step.

Run #10's owner UI said only ``node_process_start_failed`` for the joined Air.
The standing acceptor records the accept failure's code as ``last.reason``,
which ``DemoTarget._work_waiting`` reports as the member's ``detail``.  It now
carries the closed launch cause (``..._launcher_ready_timeout``), still inside
the ``[a-z][a-z0-9_]{0,95}`` code contract every hop validates.
"""

import time

from test_standing_consent import BINDING, GRANT, OFFER, POLICY, NOW, PREVIEW, Actor

import mycelium_node.process as native
from mycelium_a13_macos import run_exchange
from mycelium_a13_macos.demo_target import _code
from mycelium_a13_macos.standing_consent import (
    StandingAcceptor, bind_standing_consent, grant_standing_consent)


def _acceptor(tmp_path, error):
    grant_standing_consent(tmp_path, GRANT, policy=POLICY, now=NOW)
    bind_standing_consent(tmp_path, BINDING, now=NOW)

    def preview(actor, offer, **_kwargs):
        return {**PREVIEW, 'offer_id': offer['offer_id']}

    def accept(*_args, **_kwargs):
        raise error

    return StandingAcceptor(tmp_path, preview=preview, accept=accept, clock=lambda: NOW)


def test_accept_failure_reason_carries_the_closed_launch_cause(tmp_path):
    failure = native.NodeStartFailure.from_exception(
        phase='launcher_ready', exception=TimeoutError('x'), started=time.monotonic() - 2.0,
        budget_seconds=2.0, popen_ms=5, phase_ms={}, launcher_returncode=-15,
        launcher_exited=False)
    error = native.NodeProcessError('node_process_start_failed', start_failure=failure)
    outcome = _acceptor(tmp_path, error).tick(
        Actor(dict(OFFER)), policy=POLICY, binding=BINDING, resource_check=lambda: None)
    reason = 'node_process_start_failed_launcher_ready_timeout'
    assert outcome == {'offer_id': 'offer-1', 'state': 'failed', 'reason': reason}
    # Every later hop keeps it: the member detail code and the report contract.
    assert _code(ValueError(reason), 'work_not_accepted') == reason
    assert run_exchange._CODE.fullmatch(reason)


def test_every_closed_cause_fits_the_member_report_code_contract():
    for code in ('node_process_start_failed', 'node_process_cleanup_failed'):
        for phase in native.NODE_START_FAILURE_PHASES:
            for kind in native.NODE_START_FAILURE_KINDS:
                public = f'{code}_{phase}_{kind}'
                assert run_exchange._CODE.fullmatch(public), public


def test_a_plain_code_without_a_cause_is_unchanged(tmp_path):
    error = native.NodeProcessError('node_process_start_failed')
    outcome = _acceptor(tmp_path, error).tick(
        Actor(dict(OFFER)), policy=POLICY, binding=BINDING, resource_check=lambda: None)
    assert outcome['reason'] == 'node_process_start_failed'
