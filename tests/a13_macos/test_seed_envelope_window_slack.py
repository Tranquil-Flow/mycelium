# SPDX-License-Identifier: AGPL-3.0-only
"""The identity-envelope window stays strict, and now names its own cause.

2026-09-16 65r1 died on `seed_http_seed_time_invalid` and the rejected statement
was unrecoverable afterwards: `identity()` raises before returning it, so the run
left only the bare code, and the cause had to be reconstructed by reading source
(four hypotheses, none provable from disk; the tunnels were dead by then).

The window CONDITION follows the codebase's cross-host semantics, pinned by
`tests/seed_coordinator/test_seed_identity_clock_skew.py`: a validator behind the
issuer by up to MAX_CLOCK_SKEW_SECONDS (30 s) is accepted, beyond it rejected,
and expiry is strict. (Until two-Mac run #19 the future side had zero tolerance
and a 0.18 s inter-host skew was refused.) This test pins the other half: the
rejection carries the numbers, so the next occurrence explains itself instead
of needing archaeology.
"""
import pytest

from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPClient, SeedHTTPError, SeedHTTPServer
from mycelium_invite import SqliteInviteRegistry
from mycelium_qualification.signing import generate_ed25519_signer


def test_a_validator_behind_the_issuer_is_rejected_and_the_evidence_names_it(tmp_path):
    current = [3000.0]
    coordinator = SeedCoordinator(swarm_id='clock-test', seed_node_id='seed-clock',
        seed_url=None, signer=generate_ed25519_signer(endpoint_id='clock-endpoint'),
        invite_registry=SqliteInviteRegistry(tmp_path / 'invites.sqlite3'),
        incarnation='clock-incarnation', clock=lambda: current[0])
    with SeedHTTPServer(coordinator, host='127.0.0.1', port=0) as server:
        bundle = coordinator.mint_invite(nonce='clock-invite', ttl_seconds=120)
        client = SeedHTTPClient.from_invite_bundle(bundle, now=current[0], timeout=2)
        request = client._request

        def delayed(*args):
            current[0] = 3033.0          # the seed mints the statement at 3033
            return request(*args)

        client._request = delayed
        with pytest.raises(SeedHTTPError, match='seed_http_seed_time_invalid') as caught:
            client.identity(now=3001.0)  # the validator's clock is 32 s behind

        window = caught.value.window
        assert window is not None
        assert window['now'] == 3001.0
        assert window['issued_at'] == 3033.0
        assert window['expires_at'] > window['issued_at']
        # Positive == the validator was this far BEHIND the issuer.
        assert window['seconds_before_issued'] == pytest.approx(32.0)
        assert window['seconds_after_expires'] < 0
        assert window['max_clock_skew_seconds'] == 30.0

        # A validator within the skew, or at/after the issue time and before
        # expiry, is accepted.
        assert client.identity(now=3031.0)['seed_endpoint_id'] == 'clock-endpoint'
        accepted = client.identity(now=lambda: current[0])
        assert accepted['seed_endpoint_id'] == 'clock-endpoint'


def test_the_evidence_also_names_a_statement_whose_window_already_closed(tmp_path):
    current = [3000.0]
    coordinator = SeedCoordinator(swarm_id='clock-test', seed_node_id='seed-clock',
        seed_url=None, signer=generate_ed25519_signer(endpoint_id='clock-endpoint'),
        invite_registry=SqliteInviteRegistry(tmp_path / 'invites.sqlite3'),
        incarnation='clock-incarnation', clock=lambda: current[0])
    with SeedHTTPServer(coordinator, host='127.0.0.1', port=0) as server:
        bundle = coordinator.mint_invite(nonce='clock-invite', ttl_seconds=120)
        client = SeedHTTPClient.from_invite_bundle(bundle, now=current[0], timeout=2)
        with pytest.raises(SeedHTTPError, match='seed_http_seed_time_invalid') as caught:
            client.identity(now=lambda: 10000.0)   # far past expiry
        window = caught.value.window
        assert window['expires_at'] < window['now']
        assert window['seconds_after_expires'] > 0
        assert window['seconds_before_issued'] < 0


def test_other_codes_carry_no_window_evidence():
    error = SeedHTTPError('seed_http_remote_error')
    assert error.window is None
