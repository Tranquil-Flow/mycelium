# SPDX-License-Identifier: AGPL-3.0-only
"""A seed identity statement verified on another host tolerates ordinary clock skew.

Two-Mac demo runs #16 and #19 stopped on `seed_http_seed_time_invalid` while the
laptop's clock was ~0.18 s behind the owner Mac that runs the seed: the seed
minted the identity statement at its own `now`, and the laptop rejected it with
`now < issued_at` -- zero tolerance -- although every other cross-host signed
statement in the codebase allows `MAX_CLOCK_SKEW_SECONDS` (30 s) on the future
side and none on expiry (mycelium_membership.contracts.verify_membership_message,
route_assignment, managed_control). These tests pin those exact semantics on the
seed HTTP client: a verifier behind the issuer by up to the constant is accepted,
beyond it is rejected, and expiry stays strict.
"""
import pytest

from mycelium_invite import SqliteInviteRegistry
from mycelium_membership import MAX_CLOCK_SKEW_SECONDS
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPClient, SeedHTTPError, SeedHTTPServer

ISSUED = 3003.0


@pytest.fixture
def seed(tmp_path):
    current = [3000.0]
    coordinator = SeedCoordinator(
        swarm_id='skew-test', seed_node_id='seed-skew',
        seed_url=None, signer=generate_ed25519_signer(endpoint_id='skew-endpoint'),
        invite_registry=SqliteInviteRegistry(tmp_path / 'invites.sqlite3'),
        incarnation='skew-incarnation', clock=lambda: current[0])
    with SeedHTTPServer(coordinator, host='127.0.0.1', port=0):
        bundle = coordinator.mint_invite(nonce='skew-invite', ttl_seconds=120)
        client = SeedHTTPClient.from_invite_bundle(bundle, now=current[0], timeout=2)
        request = client._request

        def minted_at_issued(*args):
            current[0] = ISSUED          # the seed host signs the statement at ISSUED
            return request(*args)

        client._request = minted_at_issued
        yield client


def _expires(client):
    """The statement's own expiry, read from an accepted identity."""
    return float(client.identity(now=ISSUED)['expires_at'])


def test_the_reused_constant_is_the_codebase_skew():
    assert MAX_CLOCK_SKEW_SECONDS == 30.0


@pytest.mark.parametrize('seed_ahead_by', [0.2, 29.0, MAX_CLOCK_SKEW_SECONDS])
def test_a_verifier_behind_the_seed_host_within_the_skew_is_accepted(seed, seed_ahead_by):
    identity = seed.identity(now=ISSUED - seed_ahead_by)
    assert identity['seed_endpoint_id'] == 'skew-endpoint'
    assert identity['issued_at'] == ISSUED


@pytest.mark.parametrize('seed_ahead_by', [31.0, MAX_CLOCK_SKEW_SECONDS + 0.001])
def test_a_verifier_behind_the_seed_host_beyond_the_skew_is_rejected(seed, seed_ahead_by):
    with pytest.raises(SeedHTTPError, match='seed_http_seed_time_invalid') as caught:
        seed.identity(now=ISSUED - seed_ahead_by)
    window = caught.value.window
    assert window['issued_at'] == ISSUED
    assert window['seconds_before_issued'] == pytest.approx(seed_ahead_by)


def test_expiry_stays_strict_no_skew_is_added_after_expires(seed):
    expires = _expires(seed)
    assert expires > ISSUED
    assert seed.identity(now=expires)['expires_at'] == expires
    for late in (expires + 0.001, expires + 0.2, expires + 29.0):
        with pytest.raises(SeedHTTPError, match='seed_http_seed_time_invalid') as caught:
            seed.identity(now=late)
        assert caught.value.window['seconds_after_expires'] == pytest.approx(late - expires)

