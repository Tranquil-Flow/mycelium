# SPDX-License-Identifier: AGPL-3.0-or-later
"""The real owner chain behind the one-click Start (see owner_run.OwnerRun).

Each step drives the existing canonical functions -- the same ones the
operator script called over HTTP -- and waits with visible progress:

model       owner snapshot prepared; slice mode decided (relaxed D-1, Qwen3)
contributor this Mac's local contributor + its click-free agent
descriptors announce the run; every joined Mac posts its signed descriptor
observe     publish hello-only offers (or ``rebuild_with_current_members`` on
            Rebuild), wait for native hello, collect, wait for cleanup
compile     ``chain_compile.start_compile`` over the current members, with the
            members' verified capability reports (memory-proportional split)
slices      hand each member its own node slice plan; wait until verified
publish     ``chain_compile.start_serving``; members accept work
targets     every member's worker runs with model load authorized
activate    ``chain_compile.activate_serving``
serving     registry API up; chat can send prompts

The chain-compile functions are reached through ``ChainOps`` so a test (or a
later compile strategy) can replace them without touching the state machine.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any, Callable, Mapping

from .owner_run import OwnerRunError

_CODE_RE = __import__('re').compile(r'[a-z][a-z0-9_]{0,79}\Z')


#: Observation failures that no retry can change (one physical Mac, one boot).
_OBSERVATION_FINAL = frozenset({'managed_observation_hosts_not_distinct',
                                'managed_observation_boots_not_distinct'})


def _code(error: BaseException, default: str) -> str:
    code = getattr(error, 'code', None) or str(error)
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else default


class ChainOps:
    """Seam over chain_compile (N-host compile, rebuild, stop)."""

    def prepare_observation(self, app, *, run_id, deployment_id, descriptors):
        return app.action('/api/serving/observe/prepare', {'run_id': run_id, 'deployment_id': deployment_id,
                          'descriptors': descriptors, 'bootstrap_acknowledged': True})

    def rebuild(self, app, *, run_id, deployment_id, descriptors):
        from .chain_compile import rebuild_with_current_members
        return rebuild_with_current_members(app, acknowledged=True, run_id=run_id,
                                            deployment_id=deployment_id, descriptors=descriptors)

    def start_compile(self, app):
        from .chain_compile import start_compile
        return start_compile(app, acknowledged=True)

    def start_serving(self, app, proposal):
        from .chain_compile import start_serving
        return start_serving(app, proposal=proposal, acknowledged=True)

    def activate(self, app, proposal):
        from .chain_compile import activate_serving
        return activate_serving(app, proposal=proposal, acknowledged=True)

    def stop(self, app):
        from .chain_compile import stop_serving
        return stop_serving(app, timeout=30.0)


def member_capability_records(app, node_ids, *, now=None, local_device=None) -> dict[str, dict]:
    """Owner-verified capability input for the memory-proportional split.

    Joined Macs: the join lane's verified signed reports
    (``DemoApp.member_capability_reports()``).  The owner's own local
    contributor: a fresh local observation of this Mac.  Records carry the
    member's current generation and expiry so stale reports are refused.
    """
    now = time.time() if now is None else now
    reports = {}
    try:
        reports = app.member_capability_reports() or {}
    except Exception:
        reports = {}
    coordinator = app.authority.coordinator
    local = app.local_contributor.member.node_id if app.local_contributor is not None else None
    records = {}
    for node in node_ids:
        if node == local:
            if local_device is None:
                from .capability import observe_device
                local_device = observe_device(app.root)
            records[node] = {'memory_bytes': int(local_device['memory_bytes']),
                             'generation': coordinator.member(node)['generation'],
                             'expires_at': now + 900}
            continue
        report = reports.get(node)
        if not isinstance(report, Mapping) or not isinstance(report.get('device'), Mapping):
            continue
        records[node] = {'memory_bytes': report['device'].get('memory_bytes'),
                         'generation': report.get('generation'), 'expires_at': report.get('expires_at')}
    return records


def work_limits(app, proposal: Mapping[str, Any]) -> dict[str, int]:
    from .app import read_resource_policy
    policy = read_resource_policy(app.root) or {}
    return {'max_rss_bytes': int(proposal['max_combined_rss_bytes']) // 2,
            'max_disk_bytes': min(int(proposal['max_additional_disk_bytes']) // 2,
                                  int(policy.get('storage_bytes', 0)) or 1),
            'max_wall_seconds': min(int(proposal['max_wall_seconds']), 3600),
            'max_commands': 5000}


class AppDemoChain:
    def __init__(self, app, *, ops: ChainOps | None = None, budgets: Mapping[str, float] | None = None,
                 interval: float = 0.5, local_device: Callable[[], Mapping] | None = None,
                 agent_factory: Callable[..., Any] | None = None) -> None:
        self.app = app
        self.ops = ops or ChainOps()
        self.interval = interval
        self._agent_factory = agent_factory
        self.budgets = {'model': 5.0, 'descriptors': 180.0, 'bootstrap': 180.0, 'collect': 180.0,
                        'cleanup': 120.0, 'compile': 3600.0, 'slices': 7200.0, 'publish': 600.0,
                        'targets': 280.0, 'serving': 900.0, **(budgets or {})}
        self._local_device = local_device
        self.exchange = None
        self.agent = None
        self.run = None  # set by the app: the OwnerRun (for OwnerRunConsent)

    # -- helpers ------------------------------------------------------------
    def _exchange(self):
        exchange = getattr(self.app, 'run_exchange', None)
        if exchange is None:
            raise OwnerRunError('owner_exchange_unavailable')
        return exchange

    def _tick_local(self):
        if self.agent is not None:
            self.agent.tick()

    def _wait(self, ctx, predicate, *, budget, code):
        def check():
            self._tick_local()
            return predicate()
        return ctx.wait_until(check, budget=self.budgets[budget], code=code, interval=self.interval)

    def _participants(self, ctx) -> list[str]:
        return list(ctx.state.get('participants', []))

    def _reports(self, ctx) -> dict[str, dict]:
        return {n: r for n, r in self._exchange().reports().items() if r.get('run_id') == ctx.run_id}

    def _all_in(self, ctx, states) -> bool:
        reports = self._reports(ctx)
        nodes = self._participants(ctx)
        for node in nodes:
            r = reports.get(node)
            if r is None:
                return False
            if r['state'] == 'error':
                raise OwnerRunError(r.get('detail') or 'member_reported_error')
            if r['state'] not in states:
                return False
        return bool(nodes)

    def _publish_participants(self, ctx) -> None:
        reports = self._exchange().reports()
        local = ctx.state.get('local_node')
        ctx.result['participants'] = [
            {'node_id': node, 'label': 'This Mac (owner)' if node == local else 'Joined Mac',
             'state': (reports.get(node) or {}).get('state'),
             'detail': (reports.get(node) or {}).get('detail'),
             'progress': (reports.get(node) or {}).get('progress'),
             'layers': (ctx.state.get('layers') or {}).get(node)}
            for node in ctx.state.get('participants', [])]

    def _announce(self, ctx, phase, **extra) -> None:
        model = ctx.state['model']
        self._exchange().announce(phase=phase, run_id=ctx.run_id, deployment_id=ctx.deployment_id,
                                  model=model, slice_mode=ctx.state['slice_mode'],
                                  manifest_digest=ctx.state['manifest_digest'], **extra)

    def _workflow_state(self):
        workflow = getattr(self.app, '_chain_workflow', None)
        return None if workflow is None else workflow.status()

    # -- steps ----------------------------------------------------------------
    def run_step(self, name, ctx):
        getattr(self, '_step_' + name)(ctx)

    def _step_model(self, ctx):
        app = self.app
        from .serving import slice_fetch_permitted
        from .run_model import choice_for_model
        if app.installation_role != 'owner' or not app.running:
            raise OwnerRunError('owner_role_required')
        if not app.authority.status().get('swarm_created'):
            raise OwnerRunError('owner_swarm_required')
        def prepared():
            return app.acquisition.status().get('state') == 'prepared'
        ctx.progress('checking the pinned model on this Mac')
        ctx.wait_until(prepared, budget=self.budgets['model'], code='owner_model_not_prepared',
                       interval=min(self.interval, 1.0))
        source = app.acquisition.prepared_source()
        model = source.model
        slice_mode = slice_fetch_permitted(getattr(source, 'source_bytes', None), architecture=model.architecture)
        chosen = choice_for_model(model)
        ctx.state.update(prepared=source, slice_mode=bool(slice_mode),
                         manifest_digest=source.manifest_digest,
                         model={'choice': None if chosen is None else chosen.name,
                                'model_id': model.model_id, 'resolved_commit': model.resolved_commit})
        app._chain_slice_fetch = bool(slice_mode)
        ctx.progress(model.model_id + (' · each Mac fetches only its slice' if slice_mode else ' · whole snapshot per Mac'))

    def _step_contributor(self, ctx):
        app = self.app
        from .demo_target import DemoTargetAgent, OwnerRunConsent, SnapshotRangeTransport
        with app.guard:
            if app.local_contributor is None:
                app._start_local_contributor()
        local = app.local_contributor.member.node_id
        ctx.state['local_node'] = local
        exchange = self._exchange()
        source = ctx.state['prepared']
        run = self.run
        if self._agent_factory is not None:
            self.agent = self._agent_factory(app, local, exchange)
            ctx.progress('this Mac contributes as node ' + local[:12]); return
        self.agent = DemoTargetAgent(app, local=True,
            consent=OwnerRunConsent(lambda: run is not None and run.active()),
            source=lambda: exchange.announcement(local),
            sink=lambda report: exchange.submit_local(local, report),
            slice_transport=lambda: SnapshotRangeTransport(source.source_root,
                model_id=source.model.model_id, resolved_commit=source.model.resolved_commit))
        ctx.progress('this Mac contributes as node ' + local[:12])

    def _remote_members(self) -> list[str]:
        app = self.app
        own = getattr(app.authority.member, 'node_id', None)
        local = app.local_contributor.member.node_id
        now = time.time()
        nodes = []
        for member in app.authority.coordinator.members():
            if member['node_id'] in {own, local}:
                continue
            if member.get('lifecycle_state') in {'STOPPING', 'STOPPED'}:
                continue
            if member.get('lease_expires_at', 0) <= now:
                continue
            nodes.append(member['node_id'])
        return sorted(nodes)

    def _step_descriptors(self, ctx):
        exchange = self._exchange()
        exchange.forget_reports()
        self._announce(ctx, 'collect')
        local = ctx.state['local_node']
        expected = [local, *self._remote_members()]
        ctx.state['participants'] = expected

        def have():
            reports = self._reports(ctx)
            ready = [n for n in expected if (reports.get(n) or {}).get('descriptor') is not None]
            self._publish_participants(ctx)
            ctx.progress('%d of %d Macs reported' % (len(ready), len(expected)))
            return len(ready) == len(expected)
        try:
            self._wait(ctx, have, budget='descriptors', code='member_descriptors_timeout')
        except OwnerRunError:
            pass  # proceed with the Macs that reported; the compiler decides if enough
        reports = self._reports(ctx)
        cards = [reports[n]['descriptor'] for n in expected if (reports.get(n) or {}).get('descriptor')]
        ctx.state['participants'] = sorted(c['statement']['target_node_id'] for c in cards)
        ctx.state['descriptors'] = sorted(cards, key=lambda c: c['statement']['target_node_id'])
        self._publish_participants(ctx)
        if len(cards) < 2:
            raise OwnerRunError('two_macs_required')

    def _step_observe(self, ctx):
        app = self.app
        cards = ctx.state['descriptors']
        try:
            if ctx.reason == 'rebuild':
                def rebuilt():
                    state = self.ops.rebuild(app, run_id=ctx.run_id, deployment_id=ctx.deployment_id,
                                             descriptors=cards)
                    return state.get('state') == 'awaiting_bootstrap_workers'
                self._wait(ctx, rebuilt, budget='cleanup', code='rebuild_member_cleanup_timeout')
            else:
                self.ops.prepare_observation(app, run_id=ctx.run_id, deployment_id=ctx.deployment_id,
                                             descriptors=cards)
        except OwnerRunError:
            raise
        except Exception as error:
            raise OwnerRunError(_code(error, 'observe_prepare_failed')) from None
        context = app._observation_context
        ctx.state['participants'] = sorted(context.facts)
        self._announce(ctx, 'observe', per_node={n: {'bootstrap_offer_id': o['offer_id']}
                                                 for n, o in context.offers.items()})
        ctx.progress('starting native hello (no model load) on every Mac')
        self._wait(ctx, lambda: self._publish_participants(ctx) or self._all_in(ctx, {'bootstrap_running'}),
                   budget='bootstrap', code='member_bootstrap_timeout')

        last = [None]

        def collected():
            try:
                context.collect(); return True
            except Exception as error:
                code = _code(error, 'observation_collect_failed')
                if code == 'observations_already_collected':
                    return True
                # Same host / same boot is a fact about the members, not a
                # member still starting: stop now with that code.
                if code in _OBSERVATION_FINAL:
                    raise OwnerRunError(code) from None
                if code != last[0]:
                    last[0] = code
                    ctx.progress('observing hosts (retrying after %s)' % code)
                return False
        ctx.progress('observing hosts')
        self._wait(ctx, collected, budget='collect', code='member_observation_timeout')
        hosts = [o.get('host_id') for o in (context.observations or {}).get('observations', [])]
        ctx.progress('%d Macs observed, %d distinct hosts' % (len(hosts), len(set(hosts))))

        def retired():
            try:
                context.retire_bootstrap(); return True
            except Exception:
                return False
        self._wait(ctx, retired, budget='cleanup', code='member_cleanup_timeout')

    def _step_compile(self, ctx):
        app = self.app
        nodes = sorted(app._observation_context.facts)
        device = None if self._local_device is None else self._local_device()
        app._member_capability_reports = member_capability_records(app, nodes, local_device=device)
        app._chain_slice_fetch = ctx.state['slice_mode']
        self._announce(ctx, 'compile')
        try:
            self.ops.start_compile(app)
        except Exception as error:
            raise OwnerRunError(_code(error, 'chain_compile_failed')) from None
        ctx.progress('compiling the route over %d Macs' % len(nodes))

        def compiled():
            state = getattr(app, '_chain_compile_state', None) or {}
            if state.get('state') == 'failed':
                raise OwnerRunError(state.get('error_code') or 'chain_compile_failed')
            return state.get('state') == 'compiled'
        self._wait(ctx, compiled, budget='compile', code='chain_compile_timeout')
        state = app._chain_compile_state
        ctx.state['proposal'] = state['execution_proposal']
        ctx.state['layers'] = {s['node_id']: [s['start'], s['end']]
                               for s in (state.get('layer_split') or {}).get('stages', [])}
        self._publish_participants(ctx)

    def _step_slices(self, ctx):
        app = self.app
        workflow = app._chain_workflow
        plans = getattr(workflow, 'node_slice_plans', None)
        limits = work_limits(app, ctx.state['proposal'])
        ctx.state['work_limits'] = limits
        per_node = {n: {'node_slice_plan': plan} for n, plan in (plans or {}).items()}
        ctx.state['work_nodes'] = per_node
        self._announce(ctx, 'work', work_limits=limits, per_node=per_node)
        if not plans:
            ctx.progress('whole snapshots: no slice to fetch'); return

        def ready():
            self._publish_participants(ctx)
            done = sum(((p.get('progress') or {}).get('done_bytes', 0)) for p in ctx.result['participants'])
            total = sum(((p.get('progress') or {}).get('total_bytes', 0)) for p in ctx.result['participants'])
            ctx.progress('slices verified %.1f / %.1f GB' % (done / 1e9, total / 1e9))
            return self._all_in(ctx, {'slice_ready', 'work_running', 'serving'})
        self._wait(ctx, ready, budget='slices', code='member_slice_timeout')

    def _step_publish(self, ctx):
        app = self.app
        try:
            started = self.ops.start_serving(app, ctx.state['proposal'])
        except Exception as error:
            raise OwnerRunError(_code(error, 'serving_start_failed')) from None
        ctx.state['registry_origin'] = started.get('registry_origin')
        ctx.progress('work offers published')

        def awaiting():
            state = self._workflow_state() or {}
            self._publish_participants(ctx)
            if state.get('state') == 'failed':
                code = state.get('error_code') or 'managed_workflow_failed'
                self._name_waiting(ctx, code)
                raise OwnerRunError(code)
            return state.get('state') == 'awaiting_activation'
        try:
            self._wait(ctx, awaiting, budget='publish', code='member_staging_timeout')
        except OwnerRunError as error:
            if error.code == 'member_staging_timeout':
                self._name_waiting(ctx, 'timed out')
            raise

    _WORK_READY = frozenset({'work_running', 'serving'})

    def _name_waiting(self, ctx, prefix) -> bool:
        """Show which members the work steps are still waiting for, and why."""
        try:
            waiting = self._waiting_for(ctx, self._WORK_READY)
        except Exception:
            return False
        if waiting:
            ctx.progress('%s; still waiting for %s' % (prefix, '; '.join(waiting)))
        return bool(waiting)

    def _waiting_for(self, ctx, ready_states) -> list[str]:
        """'<label> <node>: <what it waits for>' for every participant not ready yet."""
        reports = self._reports(ctx)
        local = ctx.state.get('local_node')
        waiting = []
        for node in self._participants(ctx):
            report = reports.get(node) or {}
            if report.get('state') in ready_states:
                continue
            label = 'This Mac (owner)' if node == local else 'Joined Mac'
            why = report.get('detail') or report.get('state') or 'no_report'
            waiting.append('%s …%s: %s' % (label, node[-8:], why))
        return waiting

    def _step_targets(self, ctx):
        ready = self._WORK_READY

        def running():
            state = self._workflow_state() or {}
            self._publish_participants(ctx)
            done = self._all_in(ctx, ready)
            if state.get('state') == 'failed':
                code = state.get('error_code') or 'managed_workflow_failed'
                # Name the stalled members instead of a bare workflow code
                # (run 9: both Macs 'reported' until managed_workflow_activation_expired).
                if not done and self._name_waiting(ctx, code):
                    raise OwnerRunError('member_work_not_running')
                raise OwnerRunError(code)
            if not done:
                waiting = self._waiting_for(ctx, ready)
                if waiting:
                    ctx.progress('waiting for ' + '; '.join(waiting))
            return done
        try:
            self._wait(ctx, running, budget='targets', code='member_work_timeout')
        except OwnerRunError as error:
            if error.code == 'member_work_timeout':
                self._name_waiting(ctx, 'timed out')
            raise

    def _step_activate(self, ctx):
        try:
            self.ops.activate(self.app, ctx.state['proposal'])
        except Exception as error:
            raise OwnerRunError(_code(error, 'activation_failed')) from None
        ctx.progress('qualifying the route (startup challenge)')

    def _step_serving(self, ctx):
        def up():
            state = self._workflow_state() or {}
            if state.get('state') in {'failed', 'stopped'}:
                raise OwnerRunError(state.get('error_code') or 'managed_workflow_failed')
            return state.get('state') == 'api_running'
        self._wait(ctx, up, budget='serving', code='route_qualification_timeout')
        startup = self._workflow_state().get('startup') or {}
        # The supervisor reports 'http://127.0.0.1:<port>/'; chat and status
        # carry the bare origin (chat.RegistryClient accepts exactly that).
        origin = startup.get('url') or ctx.state.get('registry_origin')
        ctx.result['registry_origin'] = origin.rstrip('/') if isinstance(origin, str) else origin
        self._announce(ctx, 'serving', work_limits=ctx.state.get('work_limits'),
                       per_node=ctx.state.get('work_nodes'))
        self._publish_participants(ctx)
        ctx.progress('route ready: open the chat')

    # -- monitor / teardown ---------------------------------------------------
    def monitor(self, ctx):
        try:
            self._tick_local()  # keep this Mac's own contributor reporting while serving
        except Exception:
            pass
        state = self._workflow_state()
        if state is None:
            return 'managed_workflow_missing'
        if state['state'] in {'failed', 'stopped'}:
            return state.get('error_code') or 'managed_workflow_stopped'
        return None

    def teardown(self, ctx, *, failed):
        app = self.app
        try:
            if ctx.state.get('model') is not None:
                self._announce(ctx, 'stopped')
        except Exception:
            pass
        try:
            self.ops.stop(app)
        except Exception:
            pass
        context = getattr(app, '_observation_context', None)
        if context is not None:
            for offer in getattr(context, 'offers', {}).values():
                try:
                    app.authority.control_mailbox.request_stage_cleanup(offer)
                except Exception:
                    pass
        # Let this Mac's own contributor stop its worker and clean its staging.
        for _ in range(3):
            try:
                self._tick_local()
            except Exception:
                break
        if self.agent is not None:
            self.agent.close()
        app._observation_context = None
        app._chain_target_keys = None
        app._chain_rebuild = None
