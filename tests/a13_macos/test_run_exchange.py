# SPDX-License-Identifier: AGPL-3.0-only
"""Owner<->member demo-run exchange over the real signed local-TLS control plane."""
import json
import time

import pytest

from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.local_contributor import create_local_contributor
from mycelium_a13_macos.run_exchange import (MEMBER_REPORT, RunExchange, RunExchangeClient,
                                             validate_report)
from test_app_authority import CONSENT


def report(state='reported', **changes):
    value = {'protocol': MEMBER_REPORT, 'run_id': 'a13-x-qwen-20261013T150000Z',
             'deployment_id': 'dep-1', 'state': state, 'detail': None,
             'model': {'model_id': 'Qwen/Qwen3-0.6B', 'resolved_commit': 'c' * 40},
             'progress': None, 'descriptor': None, 'issued_at': time.time()}
    value.update(changes)
    return value


def test_report_schema_is_closed_and_bounded():
    assert validate_report(report())['state'] == 'reported'
    for bad in (report(state='hacked'), report(detail='Not A Code'), {**report(), 'extra': 1},
                report(progress={'done_bytes': -1, 'total_bytes': 1}),
                report(descriptor={'blob': 'x' * 20000}), report(run_id='../etc')):
        with pytest.raises(ValueError):
            validate_report(bad)


@pytest.fixture
def owner(tmp_path):
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    actor = None
    try:
        app.action('/api/create', {'consent': CONSENT})
        actor = create_local_contributor(app)
        yield app, actor
    finally:
        if actor is not None: actor.stop()
        app.close()


def test_member_reads_signed_announcement_and_posts_report(owner):
    app, actor = owner
    exchange = RunExchange(app.authority.control_mailbox).install()
    node = actor.member.node_id
    exchange.announce(phase='collect', run_id='a13-x-qwen-20261013T150000Z', deployment_id='dep-1',
                      model={'choice': 'qwen3-0.6b', 'model_id': 'Qwen/Qwen3-0.6B', 'resolved_commit': 'c' * 40},
                      slice_mode=True, manifest_digest='sha256:' + '1' * 64,
                      per_node={node: {'bootstrap_offer_id': 'offer-1'}, 'other-node': {'bootstrap_offer_id': 'offer-2'}})
    client = RunExchangeClient(actor.peer)
    announcement = client.read()
    assert announcement['phase'] == 'collect' and announcement['slice_mode'] is True
    assert announcement['bootstrap_offer_id'] == 'offer-1'  # only this member's entry
    assert 'offer-2' not in json.dumps(announcement)
    first = report(issued_at=time.time())
    client.report(first)
    stored = exchange.reports()[node]
    assert stored['state'] == 'reported' and stored['run_id'] == first['run_id']
    # A replayed older report never overwrites a newer one.
    client.report(report(state='work_running', issued_at=first['issued_at'] + 5))
    client.report(report(state='idle', issued_at=first['issued_at'] - 5))
    assert exchange.reports()[node]['state'] == 'work_running'


def test_report_from_a_revoked_member_is_refused(owner):
    app, actor = owner
    RunExchange(app.authority.control_mailbox).install()
    client = RunExchangeClient(actor.peer)
    client.read()
    app.action('/api/revoke', {'node_id': actor.member.node_id, 'generation': actor.member.generation})
    with pytest.raises(Exception):
        client.report(report())


def test_unknown_extension_path_is_still_unknown(owner):
    app, _ = owner
    RunExchange(app.authority.control_mailbox).install()
    with pytest.raises(Exception):
        app.authority.control_mailbox.handle_http('POST', '/managed-control/demo-run/other', {})
