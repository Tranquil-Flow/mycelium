# SPDX-License-Identifier: AGPL-3.0-only
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
import math
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping

from .owner_run import EXECUTION_CEILING_CODE, OwnerRunError, _Stopped

_CODE_RE = __import__('re').compile(r'[a-z][a-z0-9_]{0,79}\Z')


#: Observation failures that no retry can change (one physical Mac, one boot).
_OBSERVATION_FINAL = frozenset({'managed_observation_hosts_not_distinct',
                                'managed_observation_boots_not_distinct'})


def _code(error: BaseException, default: str) -> str:
    code = getattr(error, 'code', None) or str(error)
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else default


def _descriptor_left(card: Any, now: float) -> float | None:
    """Seconds until a signed descriptor expires, or ``None`` when unreadable.

    Liveness only: ``None`` passes the card on to verification, which refuses
    it by name (installation_descriptor.verify_installation_descriptor).
    """
    statement = card.get('statement') if isinstance(card, Mapping) else None
    expires = statement.get('expires_at') if isinstance(statement, Mapping) else None
    if type(expires) not in (int, float) or expires != expires:
        return None
    return float(expires) - now


def _mb(value: int) -> str:
    return '%d MB' % (value // 1_000_000)


#: A member's whole-model acquisition after its last byte (demo_target): every
#: byte is here (done == total) and the snapshot is verified, then compiled.
#: Not a download: it has its own window (verify_stall / verify_rate), never
#: the byte-stall rule -- a big model's verification on a slow disk is not a
#: stalled download.
_VERIFY_PHASES = {'model_verifying': 'verifying', 'model_compiling': 'compiling'}


def _download_note(progress: Any, first: tuple[int, float], now: float, *, phase: str | None = None) -> str:
    """'the joined Mac is downloading the model: 412 of 1519 MB, 1.1 MB/s, about 17 min left'."""
    progress = progress if isinstance(progress, Mapping) else {}
    done, total = progress.get('done_bytes'), progress.get('total_bytes')
    if phase is not None:
        size = ' (%s)' % _mb(total) if type(total) is int and total > 0 else ''
        return 'waiting for a Mac that is %s the downloaded model%s' % (_VERIFY_PHASES[phase], size)
    text = 'waiting for a Mac that is downloading the model'
    if type(done) is not int or type(total) is not int or total <= 0:
        return text
    text += ': %s of %s' % (_mb(done), _mb(total))
    elapsed = now - first[1]
    if first[0] >= 0 and elapsed > 0 and done > first[0]:
        rate = (done - first[0]) / elapsed
        left = max(0, total - done) / rate
        text += ', %.1f MB/s, about %s left' % (rate / 1e6, '%d min' % math.ceil(left / 60)
                                                 if left >= 60 else '%d s' % math.ceil(left))
    return text


def _member_failure_message(code: str, report: Any, *, stall: float, silent_max: float,
                            verify: float | None = None) -> str | None:
    progress = (report or {}).get('progress') if isinstance(report, Mapping) else None
    done = (progress or {}).get('done_bytes') if isinstance(progress, Mapping) else None
    total = (progress or {}).get('total_bytes') if isinstance(progress, Mapping) else None
    where = (' (%s of %s downloaded)' % (_mb(done), _mb(total))
             if type(done) is int and type(total) is int and total > 0 else '')
    if code == 'member_download_stalled':
        return 'The joined Mac kept reporting, but its model download made no progress for %d s%s.' % (
            stall, where)
    if code == 'member_model_verify_stalled':
        return ('The joined Mac has the whole model%s but did not finish verifying and compiling it '
                'within %d s.' % (where.replace('downloaded', 'here'), verify or 0))
    if code == 'member_unreachable':
        return ('The joined Mac stopped answering while downloading the model and did not come back '
                'within %d s%s. Check its network, then press Start again; its download resumes.' % (
                    silent_max, where))
    return None


def _owner_compiled_bundle(app):
    """The owner's compiled transfer bundle for the current run, if any.

    ``chain_compile`` writes it to ``<root>/chain-runs/<run_id>/compiled``; its
    ``deployment/*.safetensors`` are this Mac's slice files byte for byte, so the
    owner's own slice is cloned from there instead of being written again.
    """
    context = getattr(app, '_observation_context', None)
    run_id = getattr(context, 'run_id', None)
    if not isinstance(run_id, str) or not run_id:
        return None
    return Path(app.root) / 'chain-runs' / run_id / 'compiled' / 'transfer-bundle'


CLEANUP_UNPROVEN = 'router_port_shutdown_cleanup_unproven'

#: Acquisition states in which a download/verify/compile is genuinely running (acquisition.py ``status()['state']``).
ACQUISITION_ACTIVE_STATES = frozenset({'starting', 'resolving_metadata', 'checking_capacity', 'downloading',
                                       'verifying', 'compiling'})
MODEL_NOT_PREPARED_MESSAGE = ('Download the selected model on this Mac first: under Inference, tick the authorization '
                              'box, press “Download / resume source” and wait for “prepared”, then press Start again.')
MODEL_NOT_PREPARED_FIX = {'action': 'prepare_model', 'target': 'acquisition'}

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
        return stop_serving(app)

    def stop_before(self, app, *, deadline, clock, progress):
        from .chain_compile import stop_serving
        return stop_serving(app, deadline=deadline, clock=clock, progress=progress)


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
            from .standing_consent import planning_caps
            caps = planning_caps(app.root)
            if caps:
                records[node]['caps'] = caps
            if type(local_device.get('available_storage_bytes')) is int:
                records[node]['available_storage_bytes'] = local_device['available_storage_bytes']
            continue
        report = reports.get(node)
        if not isinstance(report, Mapping) or not isinstance(report.get('device'), Mapping):
            continue
        records[node] = {'memory_bytes': report['device'].get('memory_bytes'),
                         'generation': report.get('generation'), 'expires_at': report.get('expires_at')}
        if isinstance(report.get('caps'), Mapping):
            records[node]['caps'] = dict(report['caps'])
        if type(report['device'].get('available_storage_bytes')) is int:
            records[node]['available_storage_bytes'] = report['device']['available_storage_bytes']
    return records


def work_limits(app, proposal: Mapping[str, Any]) -> dict[str, int]:
    from .app import read_resource_policy
    policy = read_resource_policy(app.root) or {}
    return {'max_rss_bytes': int(proposal['max_combined_rss_bytes']) // 2,
            'max_disk_bytes': min(int(proposal['max_additional_disk_bytes']) // 2,
                                  int(policy.get('storage_bytes', 0)) or 1),
            'max_wall_seconds': min(int(proposal['max_wall_seconds']), 3600),
            'max_commands': 5000}


def member_work_limits(state: Mapping[str, Any], node_id: str, *,
                       storage_bytes: int | None = None) -> dict[str, int] | None:
    """One member's worker limits from the compiled per-split resource plan.

    Its own memory/load-peak guard and disk limit (model_resources), the disk
    capped by the storage this Mac consented to; ``None`` without a plan
    (legacy two-host compile: the run-wide ``work_limits`` apply).
    """
    from .model_resources import work_limits_for
    plan = (state or {}).get('resource_plan') or {}
    row = next((m for m in plan.get('members', []) if m.get('node_id') == node_id), None)
    return None if row is None else work_limits_for(row, storage_bytes=storage_bytes)


def _member_row(state: Mapping[str, Any], node_id: str | None) -> dict[str, Any] | None:
    plan = (state or {}).get('resource_plan') or {}
    return next((dict(m) for m in plan.get('members', []) if m.get('node_id') == node_id), None)


_RESOURCE_CODES = frozenset({'member_memory_cap_below_share', 'member_storage_cap_below_share'})


class AppDemoChain:
    def __init__(self, app, *, ops: ChainOps | None = None, budgets: Mapping[str, float] | None = None,
                 interval: float = 0.5, local_device: Callable[[], Mapping] | None = None,
                 agent_factory: Callable[..., Any] | None = None) -> None:
        self.app = app
        self.ops = ops or ChainOps()
        self.interval = interval
        self._agent_factory = agent_factory
        # descriptors: base wait; download_stall / report_silence / member_silence
        # bound a joined Mac's model download (_step_descriptors) -- no total cap;
        # its verification/compile after the last byte gets
        # max(verify_stall, total bytes / verify_rate) instead of download_stall.
        # bootstrap, collect, cleanup, targets, serving: windows WITHOUT visible
        # progress, renewed by it, with the same link-drop rule (_progress_wait).
        self.budgets = {'model': 5.0, 'descriptors': 180.0, 'download_stall': 120.0,
                        'report_silence': 30.0, 'member_silence': 300.0,
                        'verify_stall': 600.0, 'verify_rate': 25e6,
                        'descriptor_margin': 60.0, 'bootstrap': 180.0, 'collect': 180.0,
                        'cleanup': 120.0, 'compile': 3600.0, 'slices': 7200.0, 'publish': 600.0,
                        'targets': 280.0, 'serving': 900.0,
                        # V1.2 D1: how long a joined Mac with a newer identity may stay not-serving before the run
                        # ends as member_restarted (its fresh agent reports every 10 s).
                        'member_restart_grace': 15.0,
                        'rollover_margin': 120.0,
                        # V1.2 D1/D2: a Start waits this long for the previous route's close (an unproven cleanup
                        # keeps its serving thread alive until the member's commands expire).
                        'previous_close': 120.0, **(budgets or {})}
        self._local_device = local_device
        #: Injectable clock for the execution-ceiling deadline (tests).
        self._monotonic = time.monotonic
        self._sleep = time.sleep
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

    _STEP_WHAT = {'bootstrap': 'starting native hello', 'collect': 'observing hosts',
                  'cleanup': 'cleaning up the hello workers', 'targets': 'starting the work on every Mac',
                  'serving': 'qualifying the route'}

    def _progress_wait(self, ctx, predicate, *, budget, code, step,
                       waiting: Callable[[Mapping[str, Any]], list[str]] | None = None,
                       observed: Callable[[], Any] | None = None):
        """``predicate`` under a budget that renews on this step's visible progress.

        ``budgets[budget]`` bounds a stretch WITHOUT progress (it was a fixed
        total: a joined Mac on a ~1 MB/s phone hotspot is slow, not stuck).
        Progress is a value not seen before in this step of what it observes:
        the (state, detail, progress) report of each Mac it still waits for
        (``waiting(reports)``, default every participant) and ``observed()``,
        the step's own signal (the collect retry code, the workflow state).
        A report re-sent unchanged every report_interval, or a Mac flapping
        between two states, is not progress.

        Link drops as in the descriptors step: a joined Mac it waits for that
        is not heard from for report_silence is waited for while its seed lease
        is live, up to member_silence.  Silences neither count as no progress
        nor reset it; idle time since the last progress is bounded by
        member_silence + the budget.  Ends with ``code`` (stuck; the message
        names the Macs it waits for) or ``<step>_member_unreachable`` (naming
        the silent Mac).
        """
        clock = time.monotonic
        window = self.budgets[budget]
        silence = self.budgets.get('report_silence', 30.0)
        silent_max = self.budgets.get('member_silence', 300.0)
        what = self._STEP_WHAT[step]
        local = ctx.state.get('local_node')
        seen: set[tuple] = set()
        start = clock()
        track = {'rose': start, 'flat': 0.0, 'polled': start}

        def label(node: str) -> str:
            return '%s …%s' % ('This Mac (owner)' if node == local else 'Joined Mac', node[-8:])

        def check():
            self._tick_local()
            value = predicate()
            if value:
                return value
            now, wall = clock(), time.time()
            reports = self._reports(ctx)
            nodes = list(waiting(reports) if waiting is not None else self._participants(ctx))
            values = {('member', node, (reports.get(node) or {}).get('state'),
                       (reports.get(node) or {}).get('detail'),
                       json.dumps((reports.get(node) or {}).get('progress'), sort_keys=True, default=str))
                      for node in nodes}
            if observed is not None:
                values.add(('step', json.dumps(observed(), sort_keys=True, default=str)))
            silent = {}
            for node in nodes:
                heard = (reports.get(node) or {}).get('received_at')
                if node != local and type(heard) in (int, float) and wall - heard >= silence:
                    silent[node] = wall - heard
            if values - seen:
                seen.update(values)
                track.update(rose=now, flat=0.0)
            elif not silent:
                track['flat'] += now - track['polled']
            track['polled'] = now
            idle = now - track['rose']
            if silent:
                live = set(self._remote_members())
                gone = sorted(n for n in silent if n not in live)
                node = gone[0] if gone else max(silent, key=silent.get)
                if gone or silent[node] >= silent_max or idle >= silent_max + window:
                    raise OwnerRunError(step + '_member_unreachable', (
                        '%s stopped answering while %s and did not come back within %d s%s. Check its '
                        'network, then press Start again.' % (label(node), what, silent_max,
                                                               ' (its seed lease lapsed)' if gone else '')))
                ctx.progress('%s: %s has not been heard from for %d s (link drop?); waiting up to %d s'
                             % (what, label(node), silent[node], silent_max))
                return False
            if track['flat'] >= window or idle >= silent_max + window:
                why = '; '.join('%s: %s' % (label(n), (reports.get(n) or {}).get('detail')
                                            or (reports.get(n) or {}).get('state') or 'no_report')
                                for n in sorted(nodes, key=lambda n: n == local))
                raise OwnerRunError(code, 'Nothing advanced for %d s while %s%s.' % (
                    window, what, '; still waiting for ' + why if why else ''))
            return False
        return ctx.wait_until(check, budget=float('inf'), code=code, interval=self.interval)

    def _participants(self, ctx) -> list[str]:
        return list(ctx.state.get('participants', []))

    def _reports(self, ctx) -> dict[str, dict]:
        return {n: r for n, r in self._exchange().reports().items()
                if r.get('run_id') == ctx.run_id and r.get('deployment_id') == ctx.deployment_id}

    def _raise_member_refusal(self, ctx, reports):
        # Inspect every participant before checking readiness: an earlier
        # missing/unready report must not hide a later refusal. Older member
        # agents report standing-acceptor resource refusals as reported/detail.
        for node in self._participants(ctx):
            report = reports.get(node) or {}
            code = report.get('detail')
            if (report.get('state') != 'error' and code not in _RESOURCE_CODES
                    and code not in {'member_wall_cap_below_share', 'member_wall_limit_invalid'}):
                continue
            code = code or 'member_reported_error'
            message = None
            row = _member_row(ctx.state.get('compile_state') or {}, node)
            if code in _RESOURCE_CODES and row is not None:
                from .model_resources import refusal_message
                model = getattr(ctx.state.get('prepared'), 'model', None)
                message = refusal_message(code, row, model=model,
                    need=row['max_rss_bytes'] if 'memory' in code else row['max_disk_bytes'])
            raise OwnerRunError(code, message)

    def _all_in(self, ctx, states) -> bool:
        reports = self._reports(ctx)
        self._raise_member_refusal(ctx, reports)
        nodes = self._participants(ctx)
        return bool(nodes) and all((reports.get(n) or {}).get('state') in states for n in nodes)

    def _publish_participants(self, ctx) -> None:
        reports = self._reports(ctx)
        local = ctx.state.get('local_node')
        ctx.result['participants'] = [
            {'node_id': node, 'label': 'This Mac (owner)' if node == local else 'Joined Mac',
             'state': (reports.get(node) or {}).get('state'),
             'detail': (reports.get(node) or {}).get('detail'),
             'progress': (reports.get(node) or {}).get('progress'),
             'layers': (ctx.state.get('layers') or {}).get(node),
             'resources': _member_row(ctx.state.get('compile_state') or {}, node)}
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
        closing = getattr(app, '_chain_serving_thread', None)
        if closing is not None and closing.is_alive():
            ctx.progress('waiting for the previous route to finish closing')
            ctx.wait_until(lambda: not closing.is_alive(), budget=self.budgets['previous_close'],
                           code='previous_run_still_closing', interval=self.interval)
        def prepared():
            return app.acquisition.status().get('state') == 'prepared'
        def downloading():
            return app.acquisition.status().get('state') in ACQUISITION_ACTIVE_STATES
        ctx.progress('checking the pinned model on this Mac')
        # Refuse at once without a prepared model; wait (bounded) only while a download is really running,
        # so a harness that starts the run as the download finishes still passes.
        if not prepared() and not downloading():
            # After a relaunch the acquisition reads 'closed' although the verified snapshot is on disk: re-hash it
            # (nothing is fetched) rather than make the owner repeat the download (live demo 2026-10-21).
            reverify = getattr(app, 'reverify_model_snapshot', None)
            if callable(reverify):
                ctx.progress('checking the model files already on this Mac')
                try:
                    reverify()
                except Exception:
                    pass
        if not prepared() and not downloading():
            raise OwnerRunError('owner_model_not_prepared', MODEL_NOT_PREPARED_MESSAGE, MODEL_NOT_PREPARED_FIX)
        ctx.wait_until(lambda: prepared() or not downloading(), budget=self.budgets['model'],
                       code='owner_model_not_prepared', interval=min(self.interval, 1.0))
        if not prepared():
            raise OwnerRunError('owner_model_not_prepared', MODEL_NOT_PREPARED_MESSAGE, MODEL_NOT_PREPARED_FIX)
        source = app.acquisition.prepared_source()
        model = source.model
        if ctx.reason == 'rollover':
            binding = ctx.state.get('rollover_binding')
            if not binding or binding['model'] != (model.model_id, model.resolved_commit, source.manifest_digest):
                raise OwnerRunError('rollover_model_changed')
            self._check_rollover_members(binding)
        slice_mode = slice_fetch_permitted(getattr(source, 'source_bytes', None), architecture=model.architecture,
                                           model=model)
        self._check_owner_storage(model)
        chosen = choice_for_model(model)
        ctx.state.update(prepared=source, slice_mode=bool(slice_mode),
                         manifest_digest=source.manifest_digest,
                         model={'choice': None if chosen is None else chosen.name,
                                'model_id': model.model_id, 'resolved_commit': model.resolved_commit})
        app._chain_slice_fetch = bool(slice_mode)
        ctx.progress(model.model_id + (' · each Mac fetches only its slice' if slice_mode else ' · whole snapshot per Mac'))

    def _check_owner_storage(self, model) -> None:
        """The owner's storage consent must hold the selected model here."""
        from .app import read_resource_policy
        from .model_resources import model_shape, owner_storage_requirement, refusal_message
        if model_shape(model) is None:
            return
        policy = read_resource_policy(self.app.root) or {}
        cap, need = policy.get('storage_bytes'), owner_storage_requirement(model)
        if type(cap) is int and cap < need:
            raise OwnerRunError('owner_storage_consent_below_model',
                                refusal_message('owner_storage_consent_below_model', model=model,
                                                cap=cap, need=need))

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
                model_id=source.model.model_id, resolved_commit=source.model.resolved_commit,
                clone_root=_owner_compiled_bundle(app)))
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

    def _refuse_skewed_peers(self, ctx, nodes) -> None:
        """A peer whose clock is beyond the tolerance would drop the owner's short-TTL commands: refuse the route here."""
        offsets = getattr(self._exchange(), 'clock_offsets', None)
        skewed = {} if offsets is None else offsets.exceeded(nodes)
        if not skewed:
            return
        node = max(skewed, key=lambda n: abs(skewed[n]))
        seconds = skewed[node]
        raise OwnerRunError('peer_clock_skew_exceeded', (
            'The clock of the joined Mac …%s is %d s %s this Mac\'s, beyond the %d s the swarm tolerates. Turn on '
            '“Set time and date automatically” on both Macs (System Settings → General → Date & Time), then press Start '
            'again.' % (node[-8:], round(abs(seconds)), 'ahead of' if seconds > 0 else 'behind',
                        round(offsets.tolerance))))

    def _step_descriptors(self, ctx):
        exchange = self._exchange()
        exchange.forget_reports()
        self._announce(ctx, 'collect')
        local = ctx.state['local_node']
        expected = [local, *self._remote_members()]
        ctx.state['participants'] = expected

        # Base wait as before; past it, keep waiting ONLY while a member is
        # visibly downloading the model, and end with that member's own code:
        # * still reporting: its bytes must rise within download_stall; every
        #   rise renews the wait (budgets scale with measured progress -- no
        #   total cap while it advances; run 18b needed 1585 s of 1800);
        # * silent (no report for report_silence): a link drop.  Wait while its
        #   seed lease is live, up to member_silence (two-Mac run 21: the laptop
        #   on a phone hotspot lost every report and heartbeat ~110 s in, with
        #   its lease live to 408 s, and the run gave up at 252 s);
        # * otherwise it is dropped at the base budget, as before.
        clock = time.monotonic
        started = clock()
        base = self.budgets['descriptors']
        stall = self.budgets.get('download_stall', 120.0)
        silence = self.budgets.get('report_silence', 30.0)
        silent_max = self.budgets.get('member_silence', 300.0)
        verify_min = self.budgets.get('verify_stall', 600.0)
        verify_rate = self.budgets.get('verify_rate', 25e6)
        seen: dict[str, dict[str, Any]] = {}
        reasons: dict[str, str] = {}
        windows: dict[str, float] = {}

        def verify_window(progress: Any) -> float:
            total = (progress or {}).get('total_bytes') if isinstance(progress, Mapping) else None
            return max(verify_min, (total if type(total) is int else 0) / verify_rate)

        class _GiveUp(Exception):
            pass

        margin = self.budgets.get('descriptor_margin', 60.0)
        stale: set[str] = set()
        stale_since: dict[str, float] = {}

        def usable(report) -> bool:
            # A descriptor must outlive its use: observe verifies it right after
            # this step.  An aging one is not ready; its member re-signs it
            # while the run collects (DemoTargetAgent).  Two-Mac run 18b: this
            # Mac's descriptor, signed 51 s in, was verified 1585 s in, after
            # the joined Mac's download -- expired.
            card = (report or {}).get('descriptor')
            if card is None:
                return False
            left = _descriptor_left(card, time.time())
            return left is None or left >= margin

        def have():
            self._refuse_skewed_peers(ctx, expected)
            reports = self._reports(ctx)
            ready = [n for n in expected if usable(reports.get(n))]
            stale.clear()
            stale.update(n for n in expected if n not in ready and (reports.get(n) or {}).get('descriptor'))
            self._publish_participants(ctx)
            now = clock()
            wall = time.time()
            live = None
            active = resigning = False
            notes = []
            reasons.clear()
            for node in expected:
                report = reports.get(node) or {}
                if node in stale:
                    # A live member re-signs within half a descriptor's life;
                    # one stale for a whole stall budget is not coming back.
                    since = stale_since.setdefault(node, now)
                    if now - since < stall:
                        resigning = True
                    continue
                stale_since.pop(node, None)
                downloading = report.get('state') == 'downloading_model' or (
                    node in seen and report.get('state') == 'error')
                if node in ready or not downloading:
                    if report.get('state') == 'error' and report.get('detail'):
                        reasons[node] = report['detail']
                    continue
                done = (report.get('progress') or {}).get('done_bytes')
                done = done if type(done) is int else -1
                heard = report.get('received_at')
                silent_for = wall - heard if type(heard) in (int, float) else 0.0
                # Verifying/compiling after the last byte: its own phase and
                # window (bytes stay flat at the total by design).
                phase = report.get('detail') if report.get('detail') in _VERIFY_PHASES \
                    and report.get('state') == 'downloading_model' else None
                limit = stall if phase is None else verify_window(report.get('progress'))
                if phase is not None:
                    windows[node] = limit
                # Per member: last byte rise (or phase change), and the time it
                # was HEARD without one (silences do not count as a stall, and
                # do not reset it).
                track = seen.setdefault(node, {'done': done, 'rose': now, 'flat': 0.0, 'phase': phase,
                                               'polled': now, 'heard': True, 'first': (done, now)})
                if done > track['done'] or phase != track['phase']:
                    track.update(done=max(done, track['done']), rose=now, flat=0.0, phase=phase)
                elif silent_for < silence and track['heard']:
                    track['flat'] += now - track['polled']
                track['polled'], track['heard'] = now, silent_for < silence
                idle = now - track['rose']  # bounds a member alternating silence and flat reports
                if silent_for >= silence:
                    # Not heard from: a link drop, not a stalled download.
                    if live is None:
                        live = set(self._remote_members())
                    if node in live and silent_for < silent_max and idle < silent_max + limit:
                        active = True
                        notes.append('the joined Mac has not been heard from for %d s (link drop?); '
                                     'waiting up to %d s' % (silent_for, silent_max))
                    else:
                        reasons[node] = 'member_unreachable'
                    continue
                if track['flat'] < limit and idle < silent_max + limit:
                    active = True
                    notes.append(_download_note(report.get('progress'), track['first'], now, phase=phase))
                elif phase is not None:
                    reasons[node] = 'member_model_verify_stalled'
                else:
                    reasons[node] = ('member_download_stalled' if report.get('state') != 'error'
                                     else report.get('detail') or 'member_download_stalled')
            ctx.progress(('%d of %d Macs reported' % (len(ready), len(expected)))
                         + ('; ' + '; '.join(notes) if notes else
                            '; waiting for a Mac to re-sign its descriptor' if resigning else ''))
            if len(ready) == len(expected):
                return True
            if now - started >= base and not (active or resigning):
                raise _GiveUp()
            return False
        try:
            ctx.wait_until(lambda: self._tick_local() or have(), budget=float('inf'),
                           code='member_descriptors_timeout', interval=self.interval)
        except _GiveUp:
            pass  # proceed with the Macs that reported; the compiler decides if enough
        reports = self._reports(ctx)
        fresh = [n for n in expected if usable(reports.get(n))]
        cards = [reports[n]['descriptor'] for n in fresh]
        if len(cards) < 2:
            if any((reports.get(n) or {}).get('descriptor') for n in expected if n not in fresh):
                raise OwnerRunError('member_descriptor_stale')
            missing = [n for n in expected if n not in fresh and n in reasons]
            if missing:
                # Keep the joined Mac on the page with its state and progress.
                self._publish_participants(ctx)
                raise OwnerRunError(reasons[missing[0]], _member_failure_message(
                    reasons[missing[0]], reports.get(missing[0]), stall=stall, silent_max=silent_max,
                    verify=windows.get(missing[0])))
        ctx.state['participants'] = sorted(c['statement']['target_node_id'] for c in cards)
        ctx.state['descriptors'] = sorted(cards, key=lambda c: c['statement']['target_node_id'])
        self._publish_participants(ctx)
        if ctx.reason == 'rollover':
            binding = ctx.state['rollover_binding']
            self._check_rollover_members(binding)
            if ctx.state['participants'] != sorted(binding['members']):
                raise OwnerRunError('rollover_members_changed')
        else:
            source = ctx.state['prepared']
            ctx.state['rollover_binding'] = {
                'model': (source.model.model_id, source.model.resolved_commit, source.manifest_digest),
                'members': {n: self._member_identity(n) for n in ctx.state['participants']}}
        if len(cards) < 2:
            raise OwnerRunError('two_macs_required')

    def _step_observe(self, ctx):
        app = self.app
        cards = ctx.state['descriptors']
        try:
            if ctx.reason in {'rebuild', 'rollover'}:
                def rebuilt():
                    state = self.ops.rebuild(app, run_id=ctx.run_id, deployment_id=ctx.deployment_id,
                                             descriptors=cards)
                    return state.get('state') == 'awaiting_bootstrap_workers'
                self._progress_wait(ctx, rebuilt, budget='cleanup', code='rebuild_member_cleanup_timeout',
                                    step='cleanup')
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
        # Progress: a member's report advancing towards bootstrap_running.
        self._progress_wait(ctx, lambda: self._publish_participants(ctx) or self._all_in(ctx, {'bootstrap_running'}),
                            budget='bootstrap', code='member_bootstrap_timeout', step='bootstrap',
                            waiting=lambda reports: [n for n in self._participants(ctx)
                                                     if (reports.get(n) or {}).get('state') != 'bootstrap_running'])

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
        # One hello attempt covers every Mac and has no partial result: progress
        # is a new retry code (a further stage reached) or a member's report.
        self._progress_wait(ctx, collected, budget='collect', code='member_observation_timeout', step='collect',
                            observed=lambda: last[0])
        hosts = [o.get('host_id') for o in (context.observations or {}).get('observations', [])]
        ctx.progress('%d Macs observed, %d distinct hosts' % (len(hosts), len(set(hosts))))

        retire_code = [None]

        def retired():
            try:
                context.retire_bootstrap(); return True
            except Exception as error:
                retire_code[0] = _code(error, 'bootstrap_retire_failed')
                return False
        self._progress_wait(ctx, retired, budget='cleanup', code='member_cleanup_timeout', step='cleanup',
                            observed=lambda: retire_code[0])

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
            raise OwnerRunError(_code(error, 'chain_compile_failed'), getattr(error, 'message', None)) from None
        ctx.progress('compiling the route over %d Macs' % len(nodes))

        def compiled():
            state = getattr(app, '_chain_compile_state', None) or {}
            if state.get('state') == 'failed':
                raise OwnerRunError(state.get('error_code') or 'chain_compile_failed')
            return state.get('state') == 'compiled'
        self._wait(ctx, compiled, budget='compile', code='chain_compile_timeout')
        state = app._chain_compile_state
        proposal = state.get('execution_proposal')
        ceiling = ctx.state.get('execution_ceiling')
        if ceiling is not None:
            from .execution_admission import proposal_within_ceiling
            if not proposal_within_ceiling(proposal, ceiling):
                raise OwnerRunError('execution_proposal_outside_ceiling')
        ctx.state['proposal'] = proposal
        # Snapshot the same verified reports used by compilation. Unknown wall
        # caps (including old reports) get the minimum standing allowance, not
        # the owner's larger proposal. This is a bound, never consent authority.
        wall_limits, unknown = {}, []
        proposed_wall = work_limits(app, proposal)['max_wall_seconds']
        local = ctx.state.get('local_node')
        for node in nodes:
            caps = (app._member_capability_reports.get(node) or {}).get('caps') or {}
            cap = caps.get('max_wall_seconds')
            if cap is None and node == local:
                # The owner's own Mac runs under the owner's own proposal: the owner creates the
                # swarm and holds no standing grant, so its report carries no caps (device run
                # 2026-10-26: the 60 s fallback expired the owner's own worker at serving).
                cap = proposed_wall
            elif cap is None:
                cap = 60
                unknown.append(node)
            elif type(cap) is not int or not 60 <= cap <= 3600:
                raise OwnerRunError('member_wall_limit_invalid')
            wall_limits[node] = min(proposed_wall, cap)
        ctx.state['worker_wall_limits'] = wall_limits
        ctx.state['wall_cap_fallback_nodes'] = unknown
        if unknown:
            ctx.progress('Time limit unknown for %d Macs; using 60 seconds per run.' % len(unknown))
        ctx.state['compile_state'] = {'resource_plan': state.get('resource_plan'),
                                      'epoch': state.get('epoch'),
                                      'artifact_reuse_key': state.get('artifact_reuse_key')}
        ctx.result['compilation_cache_hit'] = state.get('compilation_cache_hit', False)
        ctx.result['build_seconds'] = state.get('build_seconds')
        ctx.state['layers'] = {s['node_id']: [s['start'], s['end']]
                               for s in (state.get('layer_split') or {}).get('stages', [])}
        self._publish_participants(ctx)

    def _step_slices(self, ctx):
        app = self.app
        workflow = app._chain_workflow
        plans = getattr(workflow, 'node_slice_plans', None)
        limits = work_limits(app, ctx.state['proposal'])
        ctx.state['work_limits'] = limits
        reuse = (ctx.state.get('compile_state') or {}).get('artifact_reuse_key')
        per_node = {n: {'node_slice_plan': plan, **({'artifact_reuse_key': reuse} if reuse else {})}
                    for n, plan in (plans or {}).items()}
        # Per-member limits from the compiled split: each Mac runs under its own
        # share's guard and disk limit, and can refuse a share beyond its caps.
        from .app import read_resource_policy
        from .model_resources import public_member_resources
        compiled = ctx.state.get('compile_state') or {}
        local = ctx.state.get('local_node')
        storage = (read_resource_policy(app.root) or {}).get('storage_bytes')
        for node in self._participants(ctx):
            row = _member_row(compiled, node)
            entry = per_node.setdefault(node, {})
            entry['work_limits'] = (member_work_limits(compiled, node,
                storage_bytes=storage if node == local else None) if row is not None else dict(limits))
            entry['work_limits']['max_wall_seconds'] = ctx.state['worker_wall_limits'][node]
            if row is not None:
                entry['resources'] = public_member_resources(row)
        limits['max_wall_seconds'] = min(ctx.state['worker_wall_limits'].values())
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
        workflow = getattr(app, '_chain_workflow', None)
        if workflow is not None:
            workflow.cleanup_progress = self._tick_local
        # Work can start as soon as its offer is published, before targets is entered.
        ctx.state.setdefault('execution_clock_started_at', self._monotonic())
        try:
            started = self.ops.start_serving(app, ctx.state['proposal'])
        except Exception as error:
            raise OwnerRunError(_code(error, 'serving_start_failed')) from None
        ctx.state['registry_origin'] = started.get('registry_origin')
        ctx.progress('work offers published')

        def awaiting():
            state = self._workflow_state() or {}
            self._publish_participants(ctx)
            self._raise_member_refusal(ctx, self._reports(ctx))
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
        # PV34: the earliest a native worker's wall clock can start; the execution ceiling counts from here.
        ctx.state.setdefault('execution_clock_started_at', self._monotonic())

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
            # Progress: a member's report advancing towards work_running
            # (work offer published -> accepted -> staged -> worker running).
            self._progress_wait(ctx, running, budget='targets', code='member_work_timeout', step='targets',
                                waiting=lambda reports: [n for n in self._participants(ctx)
                                                         if (reports.get(n) or {}).get('state') not in ready],
                                observed=lambda: {k: (self._workflow_state() or {}).get(k)
                                                  for k in ('state', 'error_code')})
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
        def needs_identity(workflow):
            compiled = getattr(self.app, '_chain_compile_state', None) or {}
            return ((ctx.state.get('compile_state') or {}).get('epoch') is not None
                    or 'epoch' in compiled
                    or getattr(workflow, '_progress_identity', None) is not None)
        def current(workflow):
            compiled = getattr(self.app, '_chain_compile_state', None) or {}
            epoch = (ctx.state.get('compile_state') or {}).get('epoch')
            app_epoch = getattr(self.app, '_chain_epoch', None)
            stop = getattr(workflow, 'stop_event', None)
            return (workflow is not None and workflow is getattr(self.app,'_chain_workflow',None)
                    and type(epoch) is int and type(compiled.get('epoch')) is int
                    and type(app_epoch) is int and compiled.get('state') == 'compiled'
                    and epoch == compiled.get('epoch') == app_epoch
                    and getattr(workflow,'_progress_identity',None)
                        == (ctx.run_id,ctx.deployment_id,epoch)
                    and stop is not None and not stop.is_set())
        def observed():
            workflow = getattr(self.app, '_chain_workflow', None)
            state = {} if workflow is None else workflow.status()
            view = {k:state.get(k) for k in ('state','error_code')}
            if current(workflow):
                progress = state.get('challenge_progress') or {}
                count = progress.get('verified_token_count')
                if type(count) is int and 0 < count <= 4096:
                    view['verified_token_count'] = count
            return view
        def up():
            workflow = getattr(self.app, '_chain_workflow', None)
            state = {} if workflow is None else workflow.status()
            if state.get('state') in {'failed', 'stopped'}:
                raise OwnerRunError(state.get('error_code') or 'managed_workflow_failed')
            if needs_identity(workflow) and not current(workflow):
                raise OwnerRunError('serving_workflow_identity_changed')
            return state.get('state') == 'api_running'
        # Only strictly new, verified, identity-bound indices renew this wait.
        # Prefill before the first verified response is still unobserved.
        self._progress_wait(ctx, up, budget='serving', code='route_qualification_timeout', step='serving',
                             observed=observed)
        workflow = getattr(self.app, '_chain_workflow', None)
        if workflow is None or (needs_identity(workflow) and not current(workflow)):
            raise OwnerRunError('serving_workflow_identity_changed')
        startup = (workflow.status().get('startup') or {})
        # 'api_running' only means the registry API is up: it also starts when
        # the startup route challenge failed (two-Mac run 14: the joined Mac
        # refused configure with placement_load_proof_mismatch, the registry
        # served with no qualified route, and this run claimed 'serving').
        if (startup.get('startup_qualification_complete') is not True
                or startup.get('unavailable_deployment_count') != 0):
            unavailable = startup.get('unavailable_deployment_count')
            ctx.progress('the startup route challenge did not qualify the route (%s deployment(s) '
                         'unavailable); the owner log names the reason '
                         '(mycelium.live_serve_diagnostic.v1)'
                         % (unavailable if type(unavailable) is int else 'unknown'))
            raise OwnerRunError('route_qualification_failed')
        # The supervisor reports 'http://127.0.0.1:<port>/'; chat and status
        # carry the bare origin (chat.RegistryClient accepts exactly that).
        origin = startup.get('url') or ctx.state.get('registry_origin')
        origin = origin.rstrip('/') if isinstance(origin, str) else origin
        from .workflow import ManagedServingWorkflow
        if not isinstance(workflow, ManagedServingWorkflow):
            # Existing injected/legacy workflow doubles do not expose the
            # production lifecycle lock. Preserve their old observational path;
            # no atomicity claim is made for an unrelated callback-only double.
            ctx.result['registry_origin'] = origin
            self._announce(ctx, 'serving', work_limits=ctx.state.get('work_limits'),
                           per_node=ctx.state.get('work_nodes'))
            self._publish_participants(ctx)
            ctx.progress('route ready: open the chat')
            return
        # Prepare potentially callback-bearing/serializing work before locking.
        exchange = self._exchange()
        from .run_exchange import RunExchange
        prepared = (exchange.prepare_announcement(
            phase='serving', run_id=ctx.run_id, deployment_id=ctx.deployment_id,
            model=ctx.state['model'], slice_mode=ctx.state['slice_mode'],
            manifest_digest=ctx.state['manifest_digest'],
            work_limits=ctx.state.get('work_limits'), per_node=ctx.state.get('work_nodes'))
            if type(exchange) is RunExchange else None)
        from contextlib import nullcontext
        # Lock order: app.guard -> OwnerRun._lock -> workflow._lock -> exchange._lock.
        # None of these scopes invokes RPC, a progress callback, or the exchange's
        # model-source provider. App writers install/retire under app.guard;
        # workflow.stop and run-return write under workflow._lock; OwnerRun.stop
        # writes under OwnerRun._lock. A mere final recheck outside this scope
        # would move, not close, the stale-publication window.
        with getattr(self.app, 'guard', nullcontext()):
            owner = getattr(ctx, '_run', None)
            with (owner._lock if owner is not None else nullcontext()):
                if owner is not None and ctx.stop_event.is_set():
                    # Preserve OwnerRun's existing cancellation contract: an
                    # explicit Stop is not an authority/qualification failure.
                    raise _Stopped()
                with workflow._lock:
                    if (workflow is not getattr(self.app, '_chain_workflow', None)
                            or workflow.stop_event.is_set()
                            or workflow._state != 'api_running'
                            or (needs_identity(workflow) and not current(workflow))
                            or (owner is not None and (owner._ctx is not ctx
                                or owner._state != 'running' or ctx.stop_event.is_set()))):
                        raise OwnerRunError('serving_workflow_identity_changed')
                    # Revalidate the same actual startup, not historical data from
                    # a previous status snapshot. Only local in-memory commit here.
                    live = workflow._startup or {}
                    if (live.get('startup_qualification_complete') is not True
                            or live.get('unavailable_deployment_count') != 0):
                        raise OwnerRunError('route_qualification_failed')
                    if prepared is not None:
                        exchange.commit_announcement(prepared)
                    ctx.result['registry_origin'] = origin
        if prepared is None:
            # Legacy injected exchange implementations have only announce();
            # never call an arbitrary callback under lifecycle locks. Production
            # RunExchange takes the fenced branch above. This adapter path is
            # compatibility only, not a publication-atomicity claim.
            self._announce(ctx, 'serving', work_limits=ctx.state.get('work_limits'),
                           per_node=ctx.state.get('work_nodes'))
        self._publish_participants(ctx)
        ctx.progress('route ready: open the chat')

    # -- monitor / teardown ---------------------------------------------------
    #: Native workers expire at start + ``max_wall_seconds`` - the 10 s cleanup reserve (worker.py).  The owner ends
    #: the run this long before ``max_wall_seconds``: the reserve plus room for the cancel-and-cleanup budget.
    EXECUTION_CEILING_LEAD_SECONDS = 30.0

    def _member_identity(self, node):
        member = self.app.authority.coordinator.member(node)
        return tuple(member.get(k) for k in self._IDENTITY)

    def _check_rollover_members(self, binding):
        local = self.app.local_contributor.member.node_id
        if set(binding['members']) != {local, *self._remote_members()}:
            raise OwnerRunError('rollover_members_changed')
        for node, identity in binding['members'].items():
            if self._member_identity(node) != identity:
                raise OwnerRunError('rollover_members_changed')

    @staticmethod
    def _smallest_worker_wall(ctx):
        limits = [v.get('max_wall_seconds') for v in
                  [ctx.state.get('work_limits') or {}, *[r.get('work_limits') or {}
                   for r in (ctx.state.get('work_nodes') or {}).values()]]]
        return min((v for v in limits if type(v) is int and v > 0), default=None)

    def rollover_due(self, ctx):
        # A zero margin disables continuation for bounded harness runs. The
        # independent worker wall guard is unchanged, including its reserve.
        margin = self.budgets['rollover_margin']
        started = ctx.state.get('execution_clock_started_at')
        limit = self._smallest_worker_wall(ctx)
        if margin <= 0 or started is None or limit is None:
            return False
        lead = min(max(self.EXECUTION_CEILING_LEAD_SECONDS, margin), limit / 2)
        return self._monotonic() >= started + limit - lead

    def ceiling_reached(self, ctx):
        """``'execution_ceiling_reached'`` once the workers' wall allowance is (nearly) spent, else ``None``.

        The nodes enforce ``max_wall_seconds`` on their own clocks and die mid-request; nothing tells the owner, so
        the run used to stay ``serving`` with a dead route (PV34).  The owner holds the same number, anchored at the
        start of the ``targets`` step - never later than any worker's own start - so it ends the run first: in-flight
        chat requests are cancelled with the explicit code, and ``serving`` is left.
        """
        started = ctx.state.get('execution_clock_started_at')
        limit = self._smallest_worker_wall(ctx)
        if started is None or limit is None:
            return None
        if self._monotonic() < started + limit - self.EXECUTION_CEILING_LEAD_SECONDS:
            return None
        chat = getattr(self.app, 'chat', None)
        cancel_all = getattr(chat, 'cancel_all', None)
        if cancel_all is not None and not ctx.state.get('execution_ceiling_cancelled'):
            ctx.state['execution_ceiling_cancelled'] = True
            # Off the caller's thread: the cancel is an HTTP round trip to a route that may already be gone.
            threading.Thread(target=cancel_all, args=(EXECUTION_CEILING_CODE,),
                             name='a13-ceiling-cancel', daemon=True).start()
        return EXECUTION_CEILING_CODE

    def monitor(self, ctx):
        code = self.ceiling_reached(ctx)
        if code:
            return code
        try:
            self._tick_local()  # keep this Mac's own contributor reporting while serving
        except Exception as error:
            ctx.record_exception('owner_monitor_tick_failed', error)
        state = self._workflow_state()
        if state is None:
            return 'managed_workflow_missing'
        if state['state'] in {'failed', 'stopped'}:
            return state.get('error_code') or 'managed_workflow_stopped'
        dead = self._dead_member(ctx)
        if dead is not None:
            ctx.state['monitor_error_code'] = dead
            return dead
        return self._restarted_member(ctx)

    def _dead_member(self, ctx):
        """The closed code of a participant that reports ``error`` while the route serves, else ``None``.

        A node whose control loop died (``authority_state_write_failed``: its pump could not persist an
        oversized response) answers nothing more; its member agent reports ``error`` with that code.  Without
        this the owner kept ``serving`` with ``route_ready`` true while every request ran into a dead node
        (live demo 2026-10-21).  Includes this Mac's own node.
        """
        reports = self._reports(ctx)
        for node in self._participants(ctx):
            report = reports.get(node)
            if report is not None and report.get('state') == 'error':
                detail = report.get('detail')
                return detail if isinstance(detail, str) and detail else 'member_reported_error'
        return None

    _IDENTITY = ('generation', 'incarnation', 'verification_key_digest', 'endpoint_id')

    def _restarted_member(self, ctx):
        """``'member_restarted'`` once a joined Mac came back as a newer identity and no longer serves this run.

        A member app restarted while the route serves is re-admitted (newer generation and incarnation) without its
        native worker; its fresh agent never re-stages, so every command to it expires unclaimed (V1.2 D1).  A
        re-admission whose worker survived (relay rotation) keeps reporting ``serving``, and a Mac that stopped
        serving under its old identity is not a restart; neither ends the run here.
        """
        context = getattr(self.app, '_observation_context', None)
        bound = {m['node_id']: m for m in (getattr(context, 'members', None) or ())
                 if isinstance(m, Mapping) and isinstance(m.get('node_id'), str)}
        if not bound:
            return None
        local = ctx.state.get('local_node')
        reports = self._reports(ctx)
        first_seen = ctx.state.setdefault('member_restart_seen', {})
        now = time.monotonic()
        grace = self.budgets['member_restart_grace']
        found = None
        for node, was in bound.items():
            report = reports.get(node)
            if node == local or report is None or report.get('state') == 'serving':
                first_seen.pop(node, None)
                continue
            try:
                current = self.app.authority.coordinator.member(node)
            except Exception as error:
                ctx.record_exception('owner_member_lookup_failed', error)
                first_seen.pop(node, None)   # revoked or left: the membership paths own that
                continue
            if all(current.get(k) == was.get(k) for k in self._IDENTITY):
                first_seen.pop(node, None)
                continue
            if now - first_seen.setdefault(node, now) >= grace:
                found = found or node
        return 'member_restarted' if found else None

    def _stop_deadline(self, ctx):
        from .worker import _CLEANUP_RESERVE_SECONDS
        started = ctx.state.get('execution_clock_started_at')
        limit = self._smallest_worker_wall(ctx)
        # Pre-worker failures still have a bounded owner wait. Once workers
        # exist, their earliest wall deadline (including reserve) wins.
        workflow = getattr(self.app, '_chain_workflow', None)
        allowance = getattr(workflow, 'close_wait_seconds', None)
        budget = allowance() if callable(allowance) else self.budgets['previous_close']
        deadline = self._monotonic() + budget
        if started is not None and limit is not None:
            deadline = min(deadline, started + limit - _CLEANUP_RESERVE_SECONDS)
        else:
            deadline = min(deadline, self._monotonic() + self.budgets['previous_close'])
        return deadline

    @staticmethod
    def _stop_error(error):
        # Never publish arbitrary exception text, even if it looks like a code.
        code = getattr(error, 'code', None) or str(error)
        return code if isinstance(code, str) and code in {
            'rebuild_stop_timeout', 'rebuild_stop_superseded',
            'rebuild_stop_timeout_invalid', 'rebuild_stop_deadline_invalid',
            'router_port_shutdown_cleanup_unproven',
            'router_port_shutdown_cancellation_timeout',
            'router_port_dispatcher_shutdown_timeout', 'router_port_worker_shutdown_timeout',
            'router_port_shutdown_cancellation_failed', 'managed_workflow_failed',
        } else 'owner_route_stop_failed'

    def _record_cleanup_exception(self, ctx, code, error):
        if ctx is not None:
            return ctx.record_exception(code, error)
        # Direct callers get the same redacted serving-status diagnostic.
        from .chain_compile import _record_cleanup_exception
        _record_cleanup_exception(self.app, getattr(self.app, '_chain_workflow', None), code, error)

    def _cleanup_tick(self, ctx=None):
        try:
            self._tick_local()
        except Exception as error:
            self._record_cleanup_exception(ctx, 'owner_cleanup_tick_failed', error)
            # Only a target receipt can prove cleanup; a tick cannot.

    def _target_cleanup_proven(self, workflow, *, deadline, require_offers=False, ctx=None):
        from mycelium_deadlines import CLEANUP_RECEIPT_POLL_SECONDS
        mailbox = self.app.authority.control_mailbox
        with workflow._lock:
            offers = tuple(workflow._offers.values())
        if require_offers and not offers:
            return False
        for offer in offers:
            try:
                mailbox.request_stage_cleanup(offer)
            except Exception as error:
                workflow.record_cleanup_exception('owner_cleanup_request_failed', error)
                if ctx is not None:
                    ctx.record_exception('owner_cleanup_request_failed', error)
                return False
        while True:
            pending = False
            for offer in offers:
                try:
                    mailbox.cleanup_receipt(offer)  # the signed target receipt, not request/exit
                except Exception as error:
                    code = ('offer_cleanup_unconfirmed' if getattr(error, 'code', None)
                            == 'offer_cleanup_unconfirmed' else 'owner_cleanup_receipt_failed')
                    workflow.record_cleanup_exception(code, error)
                    if ctx is not None:
                        ctx.record_exception(code, error)
                    if getattr(error, 'code', None) != 'offer_cleanup_unconfirmed':
                        return False
                    pending = True
            if self._monotonic() > deadline:
                return False
            if not pending:
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._cleanup_tick(ctx)
            self._sleep(max(0.0, min(CLEANUP_RECEIPT_POLL_SECONDS, deadline - self._monotonic())))

    def teardown(self, ctx, *, failed):
        app = self.app
        deadline = self._stop_deadline(ctx)
        workflow = getattr(app, '_chain_workflow', None)
        # Every teardown, including a plain owner Stop, must dispatch chat
        # cancellation while the loopback listener is still accepting DELETE.
        # Closing the listener first leaves the proxy waiting on its SSE read.
        chat = getattr(app, 'chat', None)
        cancel_all = getattr(chat, 'cancel_all', None)
        if callable(cancel_all):
            try:
                cancel_all('chat_switching_runs' if ctx.state.get('rollover_requested')
                           else (ctx.state.get('monitor_error_code') or 'owner_run_failed')
                           if failed else 'chat_cancelled')
            except Exception as error:
                ctx.record_exception('owner_chat_cancel_failed', error)
        from .chain_observation import release_owner_channel
        unproven = None
        try:
            state = self._workflow_state() or {}
            if state.get('error_code') == CLEANUP_UNPROVEN:
                unproven = CLEANUP_UNPROVEN
        except Exception as error:
            ctx.record_exception('owner_cleanup_status_failed', error)
        stop_proven = False
        stop_error = late_stop_error = None
        from .workflow import ManagedServingWorkflow
        try:
            stop_before = getattr(self.ops, 'stop_before', None)
            if callable(stop_before):
                stop_before(app, deadline=deadline, clock=self._monotonic, progress=lambda: self._cleanup_tick(ctx))
            else:
                self.ops.stop(app)  # legacy/injected operations
            stop_proven = True
        except Exception as error:
            stop_error = self._stop_error(error)
            ctx.record_exception(stop_error, error)
            if stop_error == CLEANUP_UNPROVEN:
                unproven = CLEANUP_UNPROVEN
            # A short outer join is not a terminal cleanup verdict. Observe the
            # same retired workflow, with its channel and local pump still alive,
            # under the ORIGINAL deadline. No new stop, run, TTL or retry budget.
            if (stop_error == 'rebuild_stop_timeout'
                    and isinstance(workflow, ManagedServingWorkflow)
                    and getattr(app, '_chain_serving_thread', None) is not None):
                from .chain_compile import await_stopped
                try:
                    await_stopped(app, workflow=workflow, retired_epoch=app._chain_epoch,
                                  deadline=deadline, clock=self._monotonic, progress=lambda: self._cleanup_tick(ctx))
                    stop_proven = True
                except Exception as late_error:
                    late_stop_error = self._stop_error(late_error)
                    ctx.record_exception(late_stop_error, late_error)
                    if late_stop_error == CLEANUP_UNPROVEN:
                        unproven = CLEANUP_UNPROVEN
        if stop_proven and isinstance(workflow, ManagedServingWorkflow):
            if not self._target_cleanup_proven(workflow, deadline=deadline,
                                               require_offers=bool(ctx.state.get('reached_serving')), ctx=ctx):
                unproven = unproven or 'owner_target_cleanup_unproven'
        # `stopped` is an instruction, not just status: target agents tear
        # down their control runtime on receipt. Keep that lane alive until
        # route cancellation and target cleanup have had their original
        # allowance to return authenticated proof. On failure/expiry still
        # announce Stop before releasing ownership; never imply proof from it.
        try:
            if ctx.state.get('model') is not None:
                self._announce(ctx, 'stopped')
        except Exception as error:
            ctx.record_exception('owner_stop_announce_failed', error)
        context = getattr(app, '_observation_context', None)
        if context is not None:
            for offer in getattr(context, 'offers', {}).values():
                try:
                    app.authority.control_mailbox.request_stage_cleanup(offer)
                except Exception as error:
                    ctx.record_exception('owner_cleanup_request_failed', error)
        # Let this Mac's own contributor stop its worker and clean its staging.
        for _ in range(3):
            try:
                self._tick_local()
            except Exception as error:
                ctx.record_exception('owner_cleanup_tick_failed', error)
                break
        if self.agent is not None:
            self.agent.close()
        # An unproven route cleanup stays recorded and reported (run error, status); what is released is only this
        # process's owner.lock, so a Stop/Start does not need the app to be quit (live demo 2026-10-20).
        released = False
        if context is not None:
            try:
                release_owner_channel(context)
                released = True
            except Exception as error:
                ctx.record_exception('owner_channel_release_failed', error)
        if stop_proven and unproven is None:
            if getattr(ctx, 'reason', None) == 'rollover' or ctx.state.get('rollover_requested'):
                from .compile_cache import discard_retired_artifacts
                try:
                    discard_retired_artifacts(app.root, ctx.run_id,
                                              getattr(app, '_chain_compilation_cache', None) or {})
                except Exception as error:
                    unproven = 'rollover_artifact_cleanup_failed'
                    ctx.record_exception(unproven, error)
            mailbox = getattr(getattr(app, 'authority', None), 'control_mailbox', None)
            finish = getattr(mailbox, 'finish_run', None)
            if callable(finish):
                finish(run_id=ctx.run_id, deployment_id=ctx.deployment_id)
        if not stop_proven:
            unproven = unproven or 'owner_route_stop_unproven'
        if unproven is not None:
            ctx.result['cleanup_unproven'] = {'code': unproven, 'owner_lock_released': released,
                                            **({'stop_error': stop_error} if stop_error else {}),
                                            **({'late_stop_error': late_stop_error}
                                               if late_stop_error and late_stop_error != stop_error else {})}
        app._observation_context = None
        app._chain_target_keys = None
        app._chain_rebuild = None
