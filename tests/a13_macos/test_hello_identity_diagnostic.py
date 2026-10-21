# SPDX-License-Identifier: AGPL-3.0-only
"""One code, three conditions: the hello-identity refusal must name which one fired.

Run 69r1 (2026-09-16, `a13-qwen-20260916T232148Z`) refused its only deployment with
`node_hello_identity_invalid`, and the serve diagnostic carried the code and nothing else. The
check can fail three very different ways -- the boot identity moved, the closed field set
changed, or the node no longer looked fresh (`state` / `peer_generation`) -- and those mean
different things: a legitimate restart of the same node versus a foreign or corrupted
response. Without the distinction the next investigation is more archaeology; with it, one read.

These tests pin the self-report. They also pin that the VERDICT is untouched: this attaches
evidence to the refusal, it does not relax the check.
"""
import pytest

from physical_inference_qualification import (
    ControllerError,
    _NODE_CONTROL_PROTOCOL,
    _hello_diagnostic,
    _hello_failures,
    _reject,
)

PEER = type('Peer', (), {'node_id': 'remote-mac-x', 'host_id': 'host-x', 'boot_id': 'boot-x'})()
FIELDS = {'protocol', 'run_id', 'deployment_id', 'node_id', 'host_id', 'process_id',
          'endpoint_id', 'peer_generation', 'state', 'route_ready', 'boot_id'}


def _identity(**overrides):
    base = {'protocol': _NODE_CONTROL_PROTOCOL, 'run_id': 'run-1', 'deployment_id': 'dep-1',
            'node_id': 'remote-mac-x', 'host_id': 'host-x', 'process_id': 42,
            'endpoint_id': None, 'peer_generation': 0, 'state': 'NEW', 'route_ready': False,
            'boot_id': 'boot-x'}
    base.update(overrides)
    return base


def _failures(identity):
    return _hello_failures(identity, set(FIELDS), peer=PEER, run_id='run-1',
                           deployment_id='dep-1')


def test_a_conforming_fresh_identity_reports_no_failures():
    """The report must not cry wolf: a good identity yields an empty list."""
    assert _failures(_identity()) == []


def test_a_node_that_is_no_longer_fresh_is_named_with_its_observed_state():
    """The restart-shaped case: the node advanced past NEW before the runtime was built."""
    reported = _failures(_identity(state='RUNNING'))
    assert any('state' in item and 'RUNNING' in item for item in reported), reported


def test_a_moved_peer_generation_is_named():
    reported = _failures(_identity(peer_generation=3))
    assert any('peer_generation' in item and '3' in item for item in reported), reported


def test_changed_field_set_names_what_is_missing_and_extra():
    without_csrf = dict(_identity())
    without_csrf.pop('boot_id')
    reported = _failures(dict(without_csrf, extra_field='x'))
    assert any('boot_id' in item and 'missing' in item for item in reported), reported
    assert any('extra_field' in item for item in reported), reported


def test_a_moved_boot_identity_is_reported_as_its_own_kind():
    detail = _hello_diagnostic(kind='boot_id_mismatch', peer_boot_id='boot-x',
                               observed_boot_id='boot-y')
    assert 'boot_id_mismatch' in detail
    assert 'boot-x' in detail and 'boot-y' in detail


def test_the_refusal_carries_the_report_and_keeps_the_same_public_code():
    detail = _hello_diagnostic(kind='shape_or_freshness', failures=['state=RUNNING'])
    with pytest.raises(ControllerError) as caught:
        _reject('node_hello_identity_invalid', diagnostic=detail)
    error = caught.value
    # The verdict is unchanged...
    assert error.code == 'node_hello_identity_invalid'
    assert str(error) == 'node_hello_identity_invalid'
    # ...and the evidence rides along, where _exception_chain_document will find it.
    assert error.diagnostic == detail


def test_a_refusal_without_a_report_still_raises_the_same_error():
    """Every other _reject caller is unaffected: diagnostic defaults to None."""
    with pytest.raises(ControllerError) as caught:
        _reject('some_other_code')
    assert caught.value.code == 'some_other_code'
    assert caught.value.diagnostic is None


def test_the_report_is_bounded():
    huge = _hello_diagnostic(failures=['x' * 5000])
    assert len(huge) <= 512
