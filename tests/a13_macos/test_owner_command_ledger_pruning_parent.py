# SPDX-License-Identifier: AGPL-3.0-only
"""The owner's durable managed-command ledger must stay bounded.

Regression (round36): the packaged run failed at its *first* observe with
``409 managed_owner_command_limit_exceeded``.  The ledger at
``<state_root>/owner/owner.json`` is a fixed path, not a per-run one, and
``commands`` was only ever appended to -- nothing ever removed a retired row.  It
had reached exactly ``MAX_OWNER_COMMANDS`` (1024) rows, all ``enqueue_confirmed``
with **zero** unresolved, so the cap refused all new work after two runs.

These tests exercise the pruning contract directly.  ``_prune_retired_commands``
reads only ``self._state``, so it is bound to a lightweight stub rather than
building a full signer/client stack; no model execution and no network here.
"""
from types import SimpleNamespace as NS

import pytest

from mycelium_a13_macos.control_session import (
    MAX_OWNER_COMMANDS,
    ManagedControlOwner,
    _COMMAND_LEDGER_TAIL,
)


def _row(confirmed=True):
    return {
        'binding': {'node': 'remote-mac-x'},
        'digest': 'sha256:' + 'a' * 64,
        'enqueue_request': {'protocol': 'x'},
        'enqueue_confirmed': confirmed,
    }


def _state(n_confirmed=0, n_unresolved=0, write_intents=None):
    commands = {}
    for index in range(n_confirmed):
        commands['cmd-confirmed-%04d' % index] = _row(True)
    for index in range(n_unresolved):
        commands['cmd-unresolved-%04d' % index] = _row(False)
    return {
        'commands': commands,
        'write_intents': len(commands) if write_intents is None else write_intents,
        'sequence': 22912,
        'sequence_ceiling': 22976,
    }


def _prune(state, keep='cmd-new'):
    ManagedControlOwner._prune_retired_commands(NS(_state=state), keep=keep)
    return state


def test_the_observed_round36_ledger_is_brought_back_under_the_cap():
    """The exact failing state: 1024 confirmed rows, 0 unresolved."""
    state = _state(n_confirmed=MAX_OWNER_COMMANDS)
    assert len(state['commands']) == MAX_OWNER_COMMANDS
    assert not any(not row['enqueue_confirmed'] for row in state['commands'].values())

    _prune(state)

    assert len(state['commands']) == _COMMAND_LEDGER_TAIL + 1
    # The cap that fired in round36 is now satisfied even before the new row lands.
    assert len(state['commands']) < MAX_OWNER_COMMANDS - 1


def test_unresolved_rows_are_never_dropped():
    """An unknown enqueue makes the remote high-water uncertain: prune nothing."""
    state = _state(n_confirmed=MAX_OWNER_COMMANDS - 1, n_unresolved=1)
    before = dict(state['commands'])

    _prune(state)

    assert state['commands'] == before


def test_sequence_high_water_is_untouched_by_pruning():
    """No sequence may ever be reused, so pruning must not move the ceiling."""
    state = _state(n_confirmed=MAX_OWNER_COMMANDS)
    _prune(state)
    assert state['sequence'] == 22912
    assert state['sequence_ceiling'] == 22976


def test_write_intents_track_the_retained_rows():
    """Otherwise the request limit becomes the next accumulation wall."""
    state = _state(n_confirmed=MAX_OWNER_COMMANDS, write_intents=MAX_OWNER_COMMANDS)
    dropped = MAX_OWNER_COMMANDS - (_COMMAND_LEDGER_TAIL + 1)

    _prune(state)

    assert state['write_intents'] == MAX_OWNER_COMMANDS - dropped
    assert state['write_intents'] == len(state['commands'])


def test_pruning_is_a_noop_below_the_tail():
    state = _state(n_confirmed=_COMMAND_LEDGER_TAIL)
    before = dict(state['commands'])
    _prune(state)
    assert state['commands'] == before


def test_the_new_command_is_never_pruned():
    """A duplicate retry of the command being issued must still be detected."""
    state = _state(n_confirmed=MAX_OWNER_COMMANDS)
    keep = 'cmd-confirmed-1023'  # present, and last inserted
    _prune(state, keep=keep)
    assert keep in state['commands']


@pytest.mark.parametrize('fill', [0, 1, 10, MAX_OWNER_COMMANDS])
def test_prune_improves_only_by_dropping_retired_rows(fill):
    state = _state(n_confirmed=fill)
    _prune(state)
    assert len(state['commands']) <= max(fill, _COMMAND_LEDGER_TAIL + 1)
