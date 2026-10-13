# SPDX-License-Identifier: AGPL-3.0-or-later
"""DemoApp glue for the one-click demo run (kept out of app.py on purpose).

Owner: ``/api/demo/start``, ``/api/demo/stop``, ``/api/demo/rebuild`` drive
``owner_run.OwnerRun`` over ``demo_chain.AppDemoChain``; the run exchange is
mounted on the authority's control mailbox and always announces the owner's
chosen model (phase ``idle``) so joined Macs know what the next run pins.

Joined Mac: ``auto_step`` is appended to ``app.auto_steps`` (the join lane's
standing-consent loop runs it every 2 s only while the standing grant holds);
``holds_offer`` keeps the standing acceptor off bootstrap offers and off work
offers whose slice is not verified yet.
"""
from __future__ import annotations

from typing import Any

OWNER_ROUTES = frozenset({'/api/demo/start', '/api/demo/stop', '/api/demo/rebuild'})


def ensure_exchange(app: Any):
    if app.installation_role != 'owner' or app.authority is None:
        return None
    mailbox = getattr(app.authority, 'control_mailbox', None)
    if mailbox is None:
        return None
    exchange = getattr(app, 'run_exchange', None)
    if exchange is None or exchange.mailbox is not mailbox:
        from .run_exchange import RunExchange
        exchange = RunExchange(mailbox).install()
        app.run_exchange = exchange
    run = getattr(app, 'owner_run', None)
    if (run is None or not run.active()) and exchange.announcement()['phase'] in {'idle', 'stopped'}:
        from .run_model import app_run_model
        name, _, model = app_run_model(app.root)
        current = exchange.announcement()
        wanted = {'choice': name, 'model_id': model.model_id, 'resolved_commit': model.resolved_commit}
        if current['phase'] != 'idle' or current.get('model') != wanted:
            exchange.announce(phase='idle', model=wanted)
    return exchange


def owner_run(app: Any):
    run = getattr(app, 'owner_run', None)
    if run is None:
        from .demo_chain import AppDemoChain
        from .owner_run import OwnerRun
        chain = AppDemoChain(app)
        run = OwnerRun(chain)
        chain.run = run
        app.owner_run = run
    return run


def action(app: Any, path: str, body: Any) -> dict:
    from .app import DemoError
    from .owner_run import OwnerRunError
    if app.installation_role != 'owner':
        raise DemoError('owner_role_required')
    if body:
        raise DemoError('invalid_request')
    if ensure_exchange(app) is None:
        raise DemoError('owner_swarm_required')
    run = owner_run(app)
    try:
        if path == '/api/demo/start':
            return run.start()
        if path == '/api/demo/stop':
            return run.stop()
        return run.rebuild_async()
    except OwnerRunError as error:
        raise DemoError(error.code) from None


def owner_status(app: Any) -> dict | None:
    if app.installation_role != 'owner':
        return None
    try:
        ensure_exchange(app)
    except Exception:
        pass
    run = getattr(app, 'owner_run', None)
    if run is None:
        from .owner_run import STEPS
        return {'state': 'idle', 'step': None, 'error_code': None, 'failed_step': None,
                'steps': [{'name': n, 'label': l, 'state': 'pending', 'detail': None} for n, l in STEPS],
                'participants': None, 'registry_origin': None, 'route_ready': False, 'run_id': None}
    return run.status()


# -- joined Mac ---------------------------------------------------------------

def target_agent(app: Any):
    agent = getattr(app, 'demo_target', None)
    if agent is None and app.installation_role == 'target' and getattr(app.authority, 'peer', None) is not None:
        from .demo_target import DemoTargetAgent, StandingConsentGate
        from .run_exchange import RunExchangeClient
        client = RunExchangeClient(app.authority.peer)
        agent = DemoTargetAgent(app, consent=StandingConsentGate(), source=client.read, sink=client.report)
        app.demo_target = agent
    return agent


def auto_step(app: Any, context: dict) -> dict | None:
    """``app.auto_steps`` entry: one click-free demo step under standing consent."""
    agent = target_agent(app)
    if agent is not None:
        agent.tick()  # its status is shown via app.status()['demo_target']
    return None


def holds_offer(app: Any) -> bool:
    agent = getattr(app, 'demo_target', None)
    return agent is not None and agent.holds_offer()


def target_status(app: Any) -> dict | None:
    if app.installation_role != 'target':
        return None
    agent = getattr(app, 'demo_target', None)
    membership = (app.authority.status() if app.authority is not None else {}).get('membership_state')
    if agent is None:
        return {'state': 'joined' if membership == 'current' else 'not_joined', 'detail': None,
                'progress': None, 'phase': None, 'run_id': None, 'model_id': None, 'local': False}
    value = agent.status()
    if value['state'] == 'idle' and membership == 'current':
        value['state'] = 'joined'
    return value


def close(app: Any) -> None:
    run = getattr(app, 'owner_run', None)
    if run is not None and run.active():
        run.stop()
        run.join(60)
    agent = getattr(app, 'demo_target', None)
    if agent is not None:
        agent.close()
