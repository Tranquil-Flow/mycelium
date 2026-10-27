# SPDX-License-Identifier: AGPL-3.0-only
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
    source = getattr(app, 'model_source_server', None)
    if source is None or source.mailbox is not mailbox:
        # Members may read the owner's verified pinned snapshot (relay route,
        # plus a LAN-direct listener during a run) instead of Hugging Face.
        from .peer_model_source import OwnerModelSource, owner_snapshot
        if source is not None:
            source.close()
        source = OwnerModelSource(mailbox, source=lambda: owner_snapshot(app)).install()
        app.model_source_server = source
    exchange.set_model_source_provider(source.hint)
    run = getattr(app, 'owner_run', None)
    if (run is None or not run.active()) and exchange.announcement()['phase'] in {'idle', 'stopped'}:
        from .run_model import app_run_model
        name, _, model = app_run_model(app.root)
        wanted = {'choice': name, 'model_id': model.model_id, 'resolved_commit': model.resolved_commit}
        # Model lookup, clock and JSON preparation can invoke arbitrary code;
        # none belongs under a lifecycle lock.
        from .run_exchange import RunExchange
        if type(exchange) is RunExchange:
            prepared = exchange.prepare_announcement(phase='idle', model=wanted)
            from .owner_run import OwnerRun
            from contextlib import nullcontext
            # The app guard also serializes owner installation; its Start takes
            # OwnerRun._lock. Serving takes app.guard -> owner -> workflow ->
            # exchange, so the idle authorization and swap are one ordered act.
            with getattr(app, 'guard', nullcontext()):
                if (getattr(app, 'authority', None) is not None
                        and app.authority.control_mailbox is mailbox
                        and getattr(app, 'run_exchange', None) is exchange
                        and getattr(app, 'model_source_server', None) is source
                        and getattr(app, 'owner_run', None) is run):
                    with (run._lock if type(run) is OwnerRun else nullcontext()):
                        if run is None or (type(run) is OwnerRun and run._state not in
                                           {'running', 'serving', 'stopping', 'failing'}):
                            exchange.commit_announcement(prepared, idle_refresh=True)
        else:
            # Callback-only injected exchanges retain observational behaviour;
            # they cannot offer an atomic conditional publication contract.
            current = exchange.announcement()
            if (run is None or not run.active()) and current['phase'] in {'idle', 'stopped'} and (
                    current['phase'] != 'idle' or current.get('model') != wanted):
                exchange.announce(phase='idle', model=wanted)
    return exchange


def owner_run(app: Any):
    run = getattr(app, 'owner_run', None)
    if run is None:
        from .demo_chain import AppDemoChain
        from .owner_run import OwnerRun
        from contextlib import nullcontext
        # Construct outside the guard; only the identity installation is a
        # lifecycle act. This closes the no-owner idle-commit/Start gap.
        chain = AppDemoChain(app)
        candidate = OwnerRun(chain)
        with getattr(app, 'guard', nullcontext()):
            run = getattr(app, 'owner_run', None)
            if run is None:
                chain.run = candidate
                app.owner_run = candidate
                run = candidate
    return run


def relay_gate(app: Any) -> None:
    """Serving only with the relay up: joined members are waiting for it (R23 6a); never a bare failure."""
    counter = getattr(app, 'members_joined_without_relay', None)  # stand-in apps in tests have none
    count = 0 if counter is None else counter()
    if count:
        from .app import DemoActionError
        raise DemoActionError(
            'relay_required',
            message=(f"{count} member{'' if count == 1 else 's'} joined, but the public relay is not running, so "
                     "they cannot serve yet. Start the public relay, then try again."),
            fix={'action': 'start_relay'})


def action(app: Any, path: str, body: Any) -> dict:
    from .app import DemoError
    from .owner_run import OwnerRunError
    if app.installation_role != 'owner':
        raise DemoError('owner_role_required')
    ceiling = None
    if path == '/api/demo/start' and type(body) is dict and set(body) == {'execution_ceiling'}:
        from .execution_admission import checked_ceiling
        try: ceiling = checked_ceiling(body['execution_ceiling'])
        except ValueError: raise DemoError('execution_ceiling_invalid') from None
    elif body:
        raise DemoError('invalid_request')
    if ensure_exchange(app) is None:
        raise DemoError('owner_swarm_required')
    if path in {'/api/demo/start', '/api/demo/rebuild'}:
        relay_gate(app)
    run = owner_run(app)
    try:
        if path == '/api/demo/start':
            return run.start() if ceiling is None else run.start(execution_ceiling=ceiling)
        if path == '/api/demo/stop':
            return run.stop()
        return run.rebuild_async()
    except OwnerRunError as error:
        raise DemoError(error.code) from None


def owner_status(app: Any, *, observe_only: bool = False) -> dict | None:
    if app.installation_role != 'owner':
        return None
    if not observe_only:
        try:
            ensure_exchange(app)
        except Exception:
            pass
    run = getattr(app, 'owner_run', None)
    if run is None:
        from .owner_run import STEPS
        return {'state': 'idle', 'step': None, 'error_code': None, 'error_message': None, 'failed_step': None,
                'steps': [{'name': n, 'label': l, 'state': 'pending', 'detail': None} for n, l in STEPS],
                'participants': None, 'registry_origin': None, 'route_ready': False, 'run_id': None}
    return run.status_snapshot() if observe_only else run.status()


# -- joined Mac ---------------------------------------------------------------

def target_agent(app: Any):
    agent = getattr(app, 'demo_target', None)
    if agent is None and app.installation_role == 'target' and getattr(app.authority, 'peer', None) is not None:
        from .demo_target import DemoTargetAgent, StandingConsentGate
        from .run_exchange import RunExchangeClient
        from .peer_model_source import joiner_model_source
        client = RunExchangeClient(app.authority.peer)
        agent = DemoTargetAgent(app, consent=StandingConsentGate(), source=client.read, sink=client.report,
                                model_source=lambda announcement: joiner_model_source(app, announcement))
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
    if app.installation_role == 'owner':
        chain = getattr(getattr(app, 'owner_run', None), 'chain', None)
        agent = getattr(chain, 'agent', None)
        return None if agent is None else agent.status_snapshot()
    if app.installation_role != 'target':
        return None
    agent = getattr(app, 'demo_target', None)
    membership = (app.authority.status() if app.authority is not None else {}).get('membership_state')
    if agent is None:
        return {'state': 'joined' if membership == 'current' else 'not_joined', 'detail': None,
                'progress': None, 'phase': None, 'run_id': None, 'model_id': None, 'local': False}
    value = agent.status_snapshot()
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
    source = getattr(app, 'model_source_server', None)
    if source is not None:
        source.close()
