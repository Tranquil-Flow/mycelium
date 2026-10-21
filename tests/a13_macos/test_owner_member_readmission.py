# SPDX-License-Identifier: AGPL-3.0-only
"""The owner session must follow a member re-admission, not refuse the command.

The same exact-binding defect fixed on the node side (`_binding_advanced`, 1e4c2ca) exists on
the owner side, at three sites in `mycelium_a13_macos/control_session.py`:

    :333  the exchange, before the enqueue intent is built
    :451  the wait loop, before every result lookup
    :468  after a terminal result, where the check exists to catch a revocation

Each compared the freshly resolved binding with `session.binding` by plain equality and raised
`managed_control_member_stale`. `_binding_from_member` includes `membership_generation` and
`member_incarnation`, so the member's own re-admission - the deliberate result of relay rotation -
read as staleness and failed the command.

Rebinding is safe because the signed enqueue intent is built from the FRESHLY resolved binding
(`:336`), never from `session.binding`, so no signed byte moves. What must NOT change is the
refusal: a rewind to an older generation, or a different member, still has to fail closed, and the
`:468` check still has to catch a revocation.
"""

import pytest

from physical_inference_qualification import ControllerError

from test_control_pump import _world
from test_control_session import connected
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from test_member_reincarnation import _newer


def owner(tmp_path, world, current_member):
    """The shared `owner()` helper hardcodes `current_member`, so build it directly here."""

    from mycelium_a13_macos.control_session import ManagedControlOwner

    return ManagedControlOwner(tmp_path, client=world['owner_client'],
                               current_member=current_member)


def test_owner_follows_a_member_re_admission(tmp_path):
    """RED before the fix: the advance was refused as staleness instead of adopted."""

    with connected(tmp_path / 'w') as (world, _w):
        node_id = world['member']['node_id']
        member = world['authority'].coordinator.member(node_id)
        current = [member]
        sender = owner(tmp_path / 'owner', world, current_member=lambda node_id: current[0])
        try:
            session = sender.session(node_id=node_id, run_id=RUN_ID,
                                     deployment_id=DEPLOYMENT_ID, timeout_seconds=2)
            hello = session.send(command_id='before-readmission', command='hello', payload={})
            assert hello['ok'] is True
            assert session.binding['membership_generation'] == member['generation']

            advanced = _newer(member)
            current[0] = advanced

            # Whatever terminal the command reaches in this harness, the session must have
            # ADOPTED the newer incarnation rather than refused it.
            with pytest.raises(ControllerError):
                session.send(command_id='after-readmission', command='hello', payload={})

            assert session.binding['membership_generation'] == advanced['generation']
            assert session.binding['member_incarnation'] == advanced['incarnation']
        finally:
            sender.close()


def test_owner_still_refuses_a_rewind(tmp_path):
    """The fail-closed half: an older generation is never a re-admission."""

    with connected(tmp_path / 'w') as (world, _w):
        node_id = world['member']['node_id']
        member = world['authority'].coordinator.member(node_id)
        current = [_newer(member, generations=2, incarnation_suffix='.newer')]
        sender = owner(tmp_path / 'owner', world, current_member=lambda node_id: current[0])
        try:
            session = sender.session(node_id=node_id, run_id=RUN_ID,
                                     deployment_id=DEPLOYMENT_ID, timeout_seconds=2)

            # A rewind to the older incarnation of the same node.
            current[0] = member

            with pytest.raises(ControllerError, match='managed_control_member_stale'):
                session.send(command_id='after-rewind', command='hello', payload={})
        finally:
            sender.close()
