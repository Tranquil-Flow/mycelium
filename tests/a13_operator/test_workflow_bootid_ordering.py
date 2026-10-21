# SPDX-License-Identifier: AGPL-3.0-only
"""The plan-binding reorder: observations BEFORE the binding, and inert if they cannot be had.

The fix in plan_binding is inert until workflow.py supplies observations. That caller change moves the
factory construction and the observation collection ABOVE the binding, which is only safe for two reasons
that this file pins at the source level:
  * `members` is built from the LIVE coordinator in both places, so the binding cannot change whose identity
    is signed;
  * the binding does not touch run_plan, so the factory's run_id/deployment_id are the same either side.
It also pins the property that makes the change safe to ship at all: if observation collection fails for any
reason, the binding is left WITHOUT observations -- its inert default -- so the round proceeds exactly as it
did before this change.
"""
import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = (REPO / 'mycelium_a13_macos/workflow.py').read_text()


def test_observations_are_collected_before_the_binding():
    """Ordering is the whole change: after the binding the values are already stale."""
    collect = SRC.index('collect_managed_hello_observations(pre_factory')
    bind = SRC.index('bound=bind_operator_plan_document(')
    assert collect < bind, 'observations must be collected BEFORE the plan is bound'


def test_the_binding_receives_them():
    assert 'observed_boot_ids=observed_boot_ids' in SRC, 'the binding is not wired to the observations'


def test_a_failed_collection_leaves_the_binding_inert():
    """If the nodes are not talking yet, the round must not change behaviour.

    Narrowed 2026-09-19: the guard is the PRODUCER'S OWN refusal type, never a bare Exception. This
    assertion previously required `except Exception`, which pinned the defect: it is what turned a
    function-local import bug (UnboundLocalError, raised before the collector could be reached) into
    `boot_id_observation_unavailable` -- an expected-looking condition -- for an entire session, in
    every bundle, with the app suite green (soak04-owner-daemon.log).
    """
    i = SRC.index('observed_boot_ids=None')
    j = SRC.index('bound=bind_operator_plan_document(')
    block = SRC[i:j]
    assert 'except ManagedObservationsError as exc:' in block, \
        'collection is not guarded by the producer refusal type'
    assert 'except Exception' not in block, \
        'a bare Exception reports programming errors as an expected unavailability'
    assert 'observed_boot_ids=None' in block[block.index('except ManagedObservationsError as exc:'):], \
        'the guard does not reset to None'


def test_the_collector_is_imported_above_its_use():
    """A function-local import written BELOW its use makes the name local for the whole scope.

    That is the 2026-09-19 defect exactly: `collect_managed_hello_observations` was imported ~20 lines
    below the call, so the call raised UnboundLocalError every round and the collection never ran.
    """
    import_line = SRC.index('from .managed_observations import')
    call_site = SRC.index('collect_managed_hello_observations(pre_factory')
    assert import_line < call_site, 'the collector is imported at or below its call site'


def test_both_members_lists_come_from_the_live_coordinator():
    """The equivalence that makes the reorder safe: nothing reads membership from the document."""
    lists = [l.strip() for l in SRC.splitlines() if 'coordinator.member(' in l]
    assert len(lists) >= 2, 'expected both sites: %r' % lists
    for line in lists:
        assert 'document' not in line.replace('document_peers', ''), 'a members list reads the document: %r' % line


def test_the_factory_scope_comes_from_the_unbound_document():
    """run_id/deployment_id pre-binding, from run_plan, which the binding never rewrites."""
    assert "pre_run=document['controller']['run_plan']" in SRC
    assert 'run_id=pre_run[' in SRC and 'deployment_id=pre_run[' in SRC
