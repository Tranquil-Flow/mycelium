# SPDX-License-Identifier: AGPL-3.0-only
"""Native route command identity scope; no process or inference proof."""
from types import SimpleNamespace as NS
import threading
from mycelium_live.route import PhysicalLiveRoute


def command(run,deployment,node='node-0'):
    route=NS(_lock=threading.RLock(),_command_sequence=0,_plan={'run_id':run,'deployment_id':deployment})
    return PhysicalLiveRoute._command_id(route,node,'hello')


def test_durable_command_identity_is_scoped_to_run_and_deployment():
    first=command('run-one','deployment-one')
    assert first!=command('run-two','deployment-one')
    assert first!=command('run-one','deployment-two')
    assert first!=command('run-one','deployment-one','node-1')
    assert first==command('run-one','deployment-one')
    assert len(first)<=128
