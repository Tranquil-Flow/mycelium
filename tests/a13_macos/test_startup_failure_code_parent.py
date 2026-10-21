# SPDX-License-Identifier: AGPL-3.0-only
"""Preserve safe typed native causes without exposing arbitrary error text."""
from mycelium_live.supervisor import _startup_reason
from physical_inference_qualification import ControllerError


def test_wrapped_controller_code_remains_visible():
    error=RuntimeError('startup_route_rejected:managed_owner_state_unavailable')
    error.__cause__=ControllerError('managed_owner_state_unavailable')
    assert _startup_reason(error)=='managed_owner_state_unavailable'


def test_arbitrary_exception_text_stays_private():
    assert _startup_reason(RuntimeError('private canary with spaces'))=='startup_route_unavailable'
