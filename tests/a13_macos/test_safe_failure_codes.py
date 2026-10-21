# SPDX-License-Identifier: AGPL-3.0-only
"""Actionable closed domain codes must not reveal exception content."""
from mycelium_a13_macos import app
from mycelium_a13_macos.peer import RemoteMacMemberError

def test_domain_code_without_exception_message():
    error=RemoteMacMemberError('peer_transport_unreachable')
    assert app.public_failure_code(error)=='peer_transport_unreachable'

def test_unknown_or_secret_exception_is_redacted():
    assert app.public_failure_code(RuntimeError('password=do-not-print'))=='action_not_completed'
    error=RemoteMacMemberError('private/path?credential=do-not-print')
    assert app.public_failure_code(error)=='action_not_completed'
