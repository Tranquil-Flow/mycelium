# SPDX-License-Identifier: AGPL-3.0-or-later
"""Click-free member side of the one-click demo run.

``DemoTargetAgent.tick()`` turns the owner's signed run announcement into the
same actions a person would click in the app -- disclose the installation
descriptor, accept the bootstrap (hello-only) offer, start bounded native
control, clean up when asked, fetch ONLY this member's slice, and (for the
owner's own local contributor) preview/accept work -- and reports its state
back to the owner.  Each action goes through the app's existing routes, so
every consent/resource/membership check those routes make still applies.

Two plug points, both deliberately small:

* ``consent``: ``allows(app, action) -> str | None`` (``None`` = allowed).
  On a joined Mac it is ``StandingConsentGate`` (join lane's standing grant);
  on the owner's own Mac it is ``OwnerRunConsent`` (the owner pressed Start).
* ``source`` / ``sink``: read the announcement / post a report
  (``run_exchange.RunExchangeClient`` on a joined Mac, the owner's in-process
  ``RunExchange`` for its local contributor).

Remote work offers are accepted by the join lane's ``StandingAcceptor``
(``app.auto_accept``); this agent only tells it to hold an offer while it is
the bootstrap offer or while this member's slice is not verified yet
(``holds_offer``).
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .run_exchange import MEMBER_REPORT

ACTIONS = frozenset({'installation_descriptor', 'bootstrap_control', 'cleanup', 'slice_fetch',
                     'work', 'model_download'})


class StandingConsentGate:
    """Joined Mac: the join lane's standing grant, bound to this membership."""

    def allows(self, app: Any, action: str) -> str | None:
        from .standing_consent import load_standing_consent
        if action not in ACTIONS:
            return 'demo_action_unknown'
        try:
            grant = load_standing_consent(app.root)
        except Exception:
            return 'standing_consent_invalid'
        if grant is None:
            return 'standing_consent_missing'
        if grant['state'] != 'granted' or grant.get('binding') is None:
            return 'standing_consent_' + ('withdrawn' if grant['state'] == 'withdrawn' else 'unbound')
        try:
            binding = app.auto_step_context()['binding']
        except Exception:
            binding = None
        if binding is None:
            return 'membership_required'
        if dict(binding) != grant['binding']:
            return 'standing_consent_owner_mismatch'
        return app._resource_check()

    def storage_cap(self, app: Any) -> int:
        from .standing_consent import load_standing_consent
        grant = load_standing_consent(app.root)
        return int(grant['caps']['storage_bytes'])


class OwnerRunConsent:
    """Owner's own Mac: allowed only while the owner's Start is active."""

    def __init__(self, active: Callable[[], bool]) -> None:
        self._active = active

    def allows(self, app: Any, action: str) -> str | None:
        if action not in ACTIONS:
            return 'demo_action_unknown'
        if not self._active():
            return 'owner_run_not_active'
        return app._resource_check()

    def storage_cap(self, app: Any) -> int:
        from .app import read_resource_policy
        return int(read_resource_policy(app.root)['storage_bytes'])


class SnapshotRangeTransport:
    """``read_range`` over the owner's own verified snapshot (no network)."""

    def __init__(self, source_root: Path, *, model_id: str, resolved_commit: str) -> None:
        self.root = Path(source_root)
        self.model_id = model_id
        self.resolved_commit = resolved_commit

    def read_range(self, model_id, commit, path, offset, length):
        if model_id != self.model_id or commit != self.resolved_commit:
            raise ValueError('slice_transport_model_mismatch')
        target = (self.root / path).resolve()
        if not str(target).startswith(str(self.root.resolve()) + '/'):
            raise ValueError('slice_transport_path_invalid')
        with target.open('rb') as handle:
            handle.seek(offset)
            return handle.read(length)


def slice_progress(node_plan: Mapping[str, Any] | None, root: Path) -> dict[str, int] | None:
    """Bytes verified so far for this member's slice (published files + journals)."""
    if not node_plan:
        return None
    total = sum(row['size_bytes'] for row in node_plan['files'])
    done = 0
    for row in node_plan['files']:
        final = root / row['path']
        if final.is_file() and final.stat().st_size == row['size_bytes']:
            done += row['size_bytes']
    work = root / '.slice-work'
    if work.is_dir():
        sizes = {}
        for row in node_plan['files']:
            for index, segment in enumerate(row['segments']):
                sizes[(row['path'], index)] = segment['length']
        for journal in work.glob('*/journal.json'):
            try:
                value = json.loads(journal.read_text('utf-8'))
                done += sum(sizes.get((value['file'], i), 0) for i in value['completed'])
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return {'done_bytes': min(done, total), 'total_bytes': total}


class DemoTargetAgent:
    def __init__(self, app: Any, *, consent: Any, source: Callable[[], Mapping[str, Any] | None],
                 sink: Callable[[Mapping[str, Any]], Any], local: bool = False,
                 slice_transport: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.time, report_interval: float = 10.0) -> None:
        self.app = app
        self.consent = consent
        self.source = source
        self.sink = sink
        self.local = local
        self._slice_transport = slice_transport
        self._clock = clock
        self._report_interval = report_interval
        self._lock = threading.RLock()
        self.announcement: dict[str, Any] | None = None
        self.state = 'idle'
        self.detail: str | None = None
        self.progress: dict[str, int] | None = None
        self._descriptor_for: tuple | None = None
        self._descriptor: dict | None = None
        self._bootstrap_done: set[str] = set()
        self._work_done: set[str] = set()
        self._slice_thread: threading.Thread | None = None
        self._slice_result: dict[str, Any] = {}
        self._slice_stop = threading.Event()
        self._last_report: tuple | None = None
        self._last_report_at = 0.0

    # -- helpers ------------------------------------------------------------
    def _actor(self):
        return self.app.local_contributor if self.local else self.app.authority

    def _node_id(self) -> str | None:
        actor = self._actor()
        member = getattr(actor, 'member', None)
        return getattr(member, 'node_id', None)

    def _cache_root(self) -> Path:
        actor = self._actor()
        if self.local:
            return Path(self.app.acquisition.state_root)
        return Path(actor.peer.state_root) / 'serving'

    def slice_root(self, run_id: str) -> Path:
        from .managed_control import _segment
        root = self._cache_root() / 'slices' / _segment(run_id)
        return root

    def _prepare_slice_root(self, run_id: str) -> Path:
        base = self._cache_root() / 'slices'
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        # One run's slice at a time: previous runs' slice files are ours to drop.
        for child in base.iterdir():
            if child.name != run_id and child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
        root = self.slice_root(run_id)
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink() or root.resolve() != root.absolute():
            raise ValueError('slice_destination_invalid')
        return root

    def _set(self, state: str, detail: str | None = None, progress: dict | None = None) -> None:
        self.state, self.detail, self.progress = state, detail, progress

    def _report(self, announcement: Mapping[str, Any] | None, *, force: bool = False) -> None:
        model = None
        if announcement and announcement.get('model'):
            model = {k: announcement['model'][k] for k in ('model_id', 'resolved_commit')}
        report = {'protocol': MEMBER_REPORT,
                  'run_id': (announcement or {}).get('run_id'),
                  'deployment_id': (announcement or {}).get('deployment_id'),
                  'state': self.state, 'detail': self.detail, 'model': model,
                  'progress': self.progress,
                  'descriptor': self._descriptor if self._descriptor_for and announcement
                  and self._descriptor_for == (announcement.get('run_id'), announcement.get('deployment_id')) else None,
                  'issued_at': self._clock()}
        key = (report['run_id'], report['state'], report['detail'], json.dumps(report['progress']),
               report['descriptor'] is not None)
        now = self._clock()
        if not force and key == self._last_report and now - self._last_report_at < self._report_interval:
            return
        self.sink(report)
        self._last_report, self._last_report_at = key, now

    def status(self) -> dict[str, Any]:
        with self._lock:
            a = self.announcement or {}
            return {'state': self.state, 'detail': self.detail, 'progress': self.progress,
                    'phase': a.get('phase'), 'run_id': a.get('run_id'),
                    'model_id': (a.get('model') or {}).get('model_id'), 'local': self.local}

    def holds_offer(self) -> bool:
        """Tell the standing acceptor to leave the pending offer to this agent."""
        with self._lock:
            a = self.announcement
            if not a or a.get('phase') in {'idle', 'stopped'}:
                return False
            if a.get('phase') not in {'work', 'serving'}:
                return True  # observe/collect/compile: bootstrap offers only
            if a.get('slice_mode') and self._slice_result.get('run_id') != a.get('run_id'):
                return True
            return False

    def _consent(self, action: str) -> bool:
        reason = self.consent.allows(self.app, action)
        if reason is not None:
            self._set('waiting_for_consent' if reason.startswith('standing_consent') else 'idle', reason)
            return False
        return True

    # -- one step -------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        with self._lock:
            try:
                announcement = self.source()
            except Exception as error:
                self._set('error', _code(error, 'demo_announcement_unavailable'))
                return self.status()
            self.announcement = None if announcement is None else dict(announcement)
            try:
                self._step(self.announcement)
            except Exception as error:
                self._set('error', _code(error, 'demo_step_failed'))
            try:
                self._report(self.announcement)
            except Exception:
                pass
            return self.status()

    def _step(self, a: Mapping[str, Any] | None) -> None:
        app = self.app
        actor = self._actor()
        node = self._node_id()
        if actor is None or node is None:
            self._set('idle', 'membership_required'); return
        phase = (a or {}).get('phase', 'idle')
        if phase in {'idle', 'stopped'}:
            self._slice_stop.set()
            cleaned = self._cleanup_if_requested(actor)
            # No owner run (idle) or the run ended (stopped): never keep a worker.
            if not cleaned and (actor.status().get('native_control') or {}).get('state') == 'running':
                app.action('/api/control/stop', {})
            self._set('stopped' if phase == 'stopped' else 'idle')
            return
        run_id, deployment_id = a['run_id'], a['deployment_id']
        # 1. Descriptor (once per run).
        if self._descriptor_for != (run_id, deployment_id):
            if not self._consent('installation_descriptor'):
                return
            self._descriptor = self._make_descriptor(a)
            self._descriptor_for = (run_id, deployment_id)
            self._slice_result = {}
            self._slice_stop = threading.Event()
            self._set('reported')
            self._report(a, force=True)
        # 2. Owner-requested cleanup (bootstrap after observation, or teardown).
        if self._cleanup_if_requested(actor):
            return
        offer = actor.pending_stage()
        # 3. Bootstrap (hello-only, model load denied).
        bootstrap = a.get('bootstrap_offer_id')
        if (offer is not None and bootstrap and offer['offer_id'] == bootstrap
                and bootstrap not in self._bootstrap_done):
            if not self._consent('bootstrap_control'):
                return
            intent = {'offer_id': offer['offer_id'], 'archive_digest': offer['archive_digest']}
            self._set('preparing')
            app.action('/api/staging/accept', {**intent, 'stage_acknowledged': True})
            app.action('/api/control/start', {**intent, 'control_acknowledged': True})
            self._bootstrap_done.add(bootstrap)
            self._set('bootstrap_running'); return
        native = actor.status().get('native_control') or {}
        if phase in {'collect', 'observe', 'compile'}:
            self._set('bootstrap_running' if native.get('state') == 'running' else 'reported'); return
        # 4. Slice (slice mode): fetch only this member's verified slice.
        node_plan = a.get('node_slice_plan')
        if a.get('slice_mode') and node_plan:
            if self._slice_result.get('run_id') != run_id:
                self._drive_slice(a, node_plan)
                if self._slice_result.get('run_id') != run_id:
                    return
        # 5. Work: remote members are accepted by the standing acceptor; the
        #    owner's own contributor previews/accepts here under the owner's Start.
        if (self.local and offer is not None and offer['run_id'] == run_id
                and offer['offer_id'] not in self._work_done and offer['offer_id'] != bootstrap
                and a.get('work_limits')):
            if not self._consent('work'):
                return
            limits = dict(a['work_limits'])
            preview = app.action('/api/work/preview', {'limits': limits, 'stage_acknowledged': True})
            app.action('/api/work/accept', {'limits': limits, 'preview': preview, 'work_acknowledged': True})
            self._work_done.add(offer['offer_id'])
            native = actor.status().get('native_control') or {}
        if native.get('state') == 'running' and native.get('model_load_authorized'):
            self._set('serving' if phase == 'serving' else 'work_running')
        elif a.get('slice_mode') and node_plan:
            self._set('slice_ready', None, self.progress)
        else:
            self._set('reported')

    def _make_descriptor(self, a: Mapping[str, Any]) -> dict[str, Any]:
        from .installation_descriptor import installation_descriptor
        if a.get('slice_mode'):
            root = self._prepare_slice_root(a['run_id'])
            return installation_descriptor(self.app, run_id=a['run_id'], deployment_id=a['deployment_id'],
                acknowledged=True, slice_source={'source_root': str(root),
                                                 'manifest_digest': a['manifest_digest']})
        prepared = self.app.acquisition.prepared_source()
        model = a.get('model') or {}
        if (prepared.model.model_id, prepared.model.resolved_commit) != (model.get('model_id'), model.get('resolved_commit')):
            raise ValueError('demo_model_mismatch')
        return installation_descriptor(self.app, run_id=a['run_id'], deployment_id=a['deployment_id'],
                                       acknowledged=True)

    def _cleanup_if_requested(self, actor: Any) -> bool:
        requested = actor.pending_cleanup()
        if requested is None:
            return False
        if not self._consent('cleanup'):
            return True
        self._set('cleaning_up')
        if (actor.status().get('native_control') or {}).get('state') == 'running':
            self.app.action('/api/control/stop', {})
        self.app.action('/api/staging/cleanup/accept', {'offer_id': requested['offer_id'],
            'archive_digest': requested['archive_digest'], 'cleanup_acknowledged': True})
        return True

    def _drive_slice(self, a: Mapping[str, Any], node_plan: Mapping[str, Any]) -> None:
        run_id = a['run_id']
        root = self.slice_root(run_id)
        thread = self._slice_thread
        if thread is not None and thread.is_alive():
            self._set('downloading_slice', None, slice_progress(node_plan, root)); return
        if self._slice_result.get('failed_run_id') == run_id:
            self._set('error', self._slice_result.get('code')); return
        if not self._consent('slice_fetch'):
            return
        cap = self.consent.storage_cap(self.app)
        expected = node_plan['node_plan_digest']
        transport = self._slice_transport() if self._slice_transport else None
        if transport is None:
            from .acquisition import _HTTPRepositoryTransport
            transport = _HTTPRepositoryTransport()
        stop = self._slice_stop

        def work():
            from .slice_acquisition import fetch_node_slice
            try:
                fetch_node_slice(node_plan, expected_node_plan_digest=expected, destination_root=root,
                                 transport=transport, max_download_bytes=cap, max_disk_bytes=cap,
                                 should_stop=stop.is_set)
                self._slice_result = {'run_id': run_id}
            except Exception as error:
                self._slice_result = {'failed_run_id': run_id, 'code': _code(error, 'slice_fetch_failed')}

        self._slice_thread = threading.Thread(target=work, name='a13-slice-fetch', daemon=True)
        self._slice_thread.start()
        self._set('downloading_slice', None, slice_progress(node_plan, root))

    def join_slice(self, timeout: float | None = None) -> None:
        thread = self._slice_thread
        if thread is not None:
            thread.join(timeout)

    def close(self) -> None:
        self._slice_stop.set()


def _code(error: BaseException, default: str) -> str:
    import re
    code = getattr(error, 'code', None) or str(error)
    return code if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', code) else default
