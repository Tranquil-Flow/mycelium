# SPDX-License-Identifier: AGPL-3.0-only
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

import copy
import json
import math
import shutil
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .run_exchange import MEMBER_REPORT
from .control_failures import TRANSIENT_CONTROL_CODES, error_http as _error_http

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

    def member_caps(self, app: Any) -> dict[str, int]:
        """The standing caps this Mac's share must fit (memory guard + storage)."""
        from .standing_consent import load_standing_consent
        caps = load_standing_consent(app.root)['caps']
        return {'storage_bytes': int(caps['storage_bytes']), 'max_memory_bytes': int(caps['max_memory_bytes'])}


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

    def member_caps(self, app: Any) -> dict[str, int]:
        """The owner's own Mac: only its storage consent caps its share."""
        return {'storage_bytes': self.storage_cap(app)}


class SnapshotRangeTransport:
    """``read_range`` over the owner's own verified snapshot (no network)."""

    def __init__(self, source_root: Path, *, model_id: str, resolved_commit: str,
                 clone_root: Path | None = None) -> None:
        self.root = Path(source_root)
        self.model_id = model_id
        self.resolved_commit = resolved_commit
        # The owner's compiled transfer bundle: its deployment files are the
        # slice files byte for byte, so the slice is cloned, not re-written.
        self.clone_root = None if clone_root is None else Path(clone_root)

    def clone_candidate(self, path: str) -> Path | None:
        """A local file to clone for ``path``; the fetcher verifies its digest."""
        if self.clone_root is None:
            return None
        relative = PurePosixPath(path)
        if relative.is_absolute() or any(part in {'', '.', '..'} for part in relative.parts):
            return None
        candidate = self.clone_root.joinpath(*relative.parts)
        return candidate if candidate.is_file() and not candidate.is_symlink() else None

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


#: Whole-snapshot download failures a link drop causes (every byte source
#: struck: owner LAN, owner relay, Hugging Face).  The member retries these
#: with a growing backoff; any other code (storage, consent, digest) is final.
_DOWNLOAD_RETRYABLE = frozenset({
    'network_error', 'source_disconnected', 'source_unavailable', 'source_incomplete',
    'model_source_busy', 'acquisition_failed'}) | (TRANSIENT_CONTROL_CODES - {'relay_backend_unavailable'})
# Preserve the existing download policy; staging retry is a separate operation.
_RETRY_FIRST_SECONDS = 5.0
_RETRY_MAX_SECONDS = 60.0

class DemoTargetAgent:
    def __init__(self, app: Any, *, consent: Any, source: Callable[[], Mapping[str, Any] | None],
                 sink: Callable[[Mapping[str, Any]], Any], local: bool = False,
                 slice_transport: Callable[[], Any] | None = None,
                 model_source: Callable[[Mapping[str, Any]], Any] | None = None,
                 clock: Callable[[], float] = time.time, report_interval: float = 10.0) -> None:
        self.app = app
        self.consent = consent
        self.source = source
        self.sink = sink
        self.local = local
        self._slice_transport = slice_transport
        self._model_source = model_source
        self._sources: Any = None
        self._clock = clock
        self._report_interval = report_interval
        self._lock = threading.RLock()
        self.announcement: dict[str, Any] | None = None
        self.state = 'idle'
        self.detail: str | None = None
        self.error_http: dict[str, Any] | None = None
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
        self._capability_report_for = None
        self._download_for: tuple | None = None
        self._retry_delay = 0.0
        self._retry_at: float | None = None
        self._status_snapshot = self.status()

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
        # Keep at most one verified previous slice until the new signed work
        # announcement proves the same model/member/split/key binding. No copy.
        cache = getattr(self.app, '_demo_slice_cache', None) or {}
        retained = cache.get('root')
        for child in base.iterdir():
            if child.name != run_id and str(child) != retained and child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
        root = self.slice_root(run_id)
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink() or root.resolve() != root.absolute():
            raise ValueError('slice_destination_invalid')
        return root

    def _set(self, state: str, detail: str | None = None, progress: dict | None = None) -> None:
        self.state, self.detail, self.progress = state, detail, progress
        self.error_http = None

    def _set_error(self, error: Exception, default: str) -> None:
        code = _code(error, default)
        self._set('error', code)
        self.error_http = _error_http(error, code)

    def _set_step_error(self, error: Exception) -> None:
        from .managed_control import ManagedControlError

        a = self.announcement or {}
        # Discovery reads are observations, not the native worker's liveness
        # authority. A lost response used to become a fatal member report and
        # make the owner stop both healthy workers. Do not replay any operation
        # here: the next ordinary tick reads again, under unchanged deadlines.
        # HTTP refusals, policy guards, unknown causes and writes remain fatal.
        if (a.get('phase') == 'serving' and isinstance(error, ManagedControlError)
                and error.status is None
                and error.code in {'managed_control_unreachable', 'managed_control_response_timeout'}
                and error.cause_code in {None, 'dns_resolution_failed', 'connect_timeout',
                                         'relay_connect_timeout', 'relay_connect_unavailable'}
                and error.request_path in {'/managed-control/staging/read',
                                           '/managed-control/staging/cleanup/read'}):
            actor = self._actor()
            runtime = getattr(actor, '_runtime', None)
            if (runtime is not None and runtime.run_id == a.get('run_id')
                    and runtime.deployment_id == a.get('deployment_id')):
                try:
                    native = actor.status().get('native_control') or {}
                except Exception as status_error:
                    self._set_error(status_error, 'native_status_unavailable')
                    return
                if native.get('state') == 'blocked':
                    self._set('error', _code(ValueError(native.get('error_code') or ''),
                                             'native_worker_blocked'))
                    return
                if (native.get('state') == 'running' and native.get('worker_running') is True
                        and (native.get('work_admitted') or native.get('model_load_authorized'))):
                    # Do not claim a successful serving observation. Preserve
                    # the read failure separately from a dead-worker report;
                    # the route/pump/lease/worker guards still own termination.
                    self._set('reported', error.code)
                    self.error_http = _error_http(error, error.code)
                    return
        self._set_error(error, 'demo_step_failed')

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
                    'error_http': None if self.error_http is None else dict(self.error_http),
                    'phase': a.get('phase'), 'run_id': a.get('run_id'),
                    'model_id': (a.get('model') or {}).get('model_id'), 'local': self.local,
                    'sources': self._source_status()}

    def _source_status(self) -> list[dict[str, Any]] | None:
        """Which byte sources served this member (owner LAN / relay / Hugging Face)."""
        try:
            return None if self._sources is None else self._sources.status()
        except Exception:
            return None

    def status_snapshot(self) -> dict[str, Any]:
        """Last completed tick; owner status must not take the agent's I/O lock.

        The owner HTTP handler holds app.guard, which a local tick may need.
        Publish by replacement and detach reads, never mutate this snapshot.
        """
        return copy.deepcopy(self._status_snapshot)

    def _record_status(self) -> dict[str, Any]:
        value = self.status()
        self._status_snapshot = copy.deepcopy(value)
        return value

    def member_limits(self, offer: Mapping[str, Any] | None) -> dict[str, int] | None:
        """This Mac's share-derived worker limits for ``offer``'s run (StandingAcceptor)."""
        with self._lock:
            a = self.announcement or {}
            if (not isinstance(offer, Mapping) or offer.get('run_id') != a.get('run_id')
                    or not a.get('member_resources') or not isinstance(a.get('work_limits'), Mapping)):
                return None
            return dict(a['work_limits'])

    def _share_refusal(self, a: Mapping[str, Any]) -> str | None:
        """A closed code when this Mac's caps cannot hold its announced share."""
        from .model_resources import check_member_caps
        share = a.get('member_resources')
        caps_of = getattr(self.consent, 'member_caps', None)
        if not isinstance(share, Mapping) or caps_of is None:
            return None
        return check_member_caps(share, caps_of(self.app))

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
                self._set_error(error, 'demo_announcement_unavailable')
                return self._record_status()
            self.announcement = None if announcement is None else dict(announcement)
            try:
                self._step(self.announcement)
            except Exception as error:
                self._set_step_error(error)
            try:
                self._report(self.announcement)
            except Exception:
                pass
            return self._record_status()

    def _refresh_report(self, a, actor, node, *, cleanup_checked=False):
        report = getattr(self.app, 'auto_report', None)
        if self.local or not callable(report):
            return True
        member = getattr(actor, 'member', None)
        identity = (node, getattr(member, 'generation', None), getattr(member, 'incarnation', None),
                    (a or {}).get('owner_incarnation'), (a or {}).get('run_id'),
                    (a or {}).get('deployment_id'))
        if identity == self._capability_report_for:
            return True
        # Reporting may fail or require consent; teardown must never depend on it.
        if not cleanup_checked and self._cleanup_if_requested(actor):
            return False
        if not self._consent('installation_descriptor'):
            return False
        purpose = 'periodic' if (a or {}).get('phase', 'idle') in {'idle', 'stopped'} else 'pre_run'
        result = report(purpose)
        if not isinstance(result, Mapping) or result.get('state') != 'delivered':
            self._set('error', 'member_capability_report_required')
            return False
        self._capability_report_for = identity
        return True

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
            self._refresh_report(a, actor, node, cleanup_checked=True)
            return
        if not self._refresh_report(a, actor, node):
            return
        run_id, deployment_id = a['run_id'], a['deployment_id']
        # 0. Whole-snapshot mode (small models): this Mac needs the owner's whole
        #    pinned snapshot before it can describe its installation.
        if (not a.get('slice_mode') and not self.local
                and self._descriptor_for != (run_id, deployment_id)
                and not self._ensure_whole_model(a)):
            return
        # 1. Descriptor (once per run).
        if self._descriptor_for != (run_id, deployment_id):
            self._slice_stop.set()
            if self._slice_thread is not None and self._slice_thread.is_alive():
                self._set('cleaning_up')
                return
            if not self._consent('installation_descriptor'):
                return
            self._descriptor = self._make_descriptor(a)
            self._descriptor_for = (run_id, deployment_id)
            self._bootstrap_done.clear()
            self._work_done.clear()
            self._slice_result = {}
            self._slice_stop = threading.Event()
            self._set('reported')
            self._report(a, force=True)
        elif phase == 'collect' and self._descriptor_aging():
            # 1b. While the owner still collects, re-sign before half the
            # descriptor's life is gone: another Mac's long model download
            # must not expire this one before observe verifies it (two-Mac
            # run 18b).  Each descriptor keeps its own short expiry.
            if not self._consent('installation_descriptor'):
                return
            self._descriptor = self._make_descriptor(a)
            self._set('reported')
            self._report(a, force=True)
        # 2. Owner-requested cleanup (bootstrap after observation, or teardown).
        if self._cleanup_if_requested(actor):
            return
        try:
            offer = actor.pending_stage()
        except Exception as error:
            # The owner can request cleanup between the separate cleanup and
            # stage reads. Confirm the request through its signed cleanup lane;
            # any unmatched stage failure remains an error.
            if (_code(error, '') != 'offer_cleanup_requested'
                    or not self._cleanup_if_requested(actor)):
                raise
            return
        # 3. Bootstrap (hello-only, model load denied).
        bootstrap = a.get('bootstrap_offer_id')
        if (offer is not None and bootstrap and offer['offer_id'] == bootstrap
                and bootstrap not in self._bootstrap_done):
            if not self._consent('bootstrap_control'):
                return
            intent = {'offer_id': offer['offer_id'], 'archive_digest': offer['archive_digest']}
            self._set('preparing')
            try:
                app.action('/api/staging/accept', {**intent, 'stage_acknowledged': True})
            except Exception as error:
                if _code(error, '') != 'stage_runtime_active':
                    raise
                # The previous run's runtime is still attached: its worker exited, but only a cleanup request
                # detaches it and none can be made for an expired work offer (V1.2 D5).  Detach it once and retry;
                # a runtime that stays attached is reported by name.
                app.action('/api/control/stop', {})
                app.action('/api/staging/accept', {**intent, 'stage_acknowledged': True})
            app.action('/api/control/start', {**intent, 'control_acknowledged': True})
            self._bootstrap_done.add(bootstrap)
            self._set('bootstrap_running'); return
        native = actor.status().get('native_control') or {}
        if phase in {'collect', 'observe', 'compile'}:
            self._set('bootstrap_running' if native.get('state') == 'running' else 'reported'); return
        # 4. This Mac's share must fit its caps: refuse by name before any fetch.
        refusal = self._share_refusal(a)
        if refusal is not None:
            self._set('error', refusal); return
        # 5. Slice (slice mode): fetch only this member's verified slice.
        node_plan = a.get('node_slice_plan')
        if a.get('slice_mode') and node_plan:
            if self._slice_result.get('run_id') != run_id:
                self._drive_slice(a, node_plan)
                if self._slice_result.get('run_id') != run_id:
                    return
        # 6. Work: remote members are accepted by the standing acceptor; the
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
        if phase == 'serving' and native.get('state') == 'blocked':
            # The node's control loop is over (its pump could not persist state, a signed request conflicted, ...):
            # nothing will claim another command, so this Mac no longer serves.  Report it by its own closed code
            # instead of waiting; before 'serving' a blocked member keeps waiting (it can still be replaced).
            self._set('error', _code(ValueError(native.get('error_code') or ''), 'native_worker_blocked')); return
        if native.get('state') == 'running' and (native.get('work_admitted') or native.get('model_load_authorized')):
            # Work accepted and its worker is up.  The model load itself is
            # admitted only by the owner's configure AFTER activation, so waiting
            # for model_load_authorized here would deadlock the owner's targets step.
            self._set('serving' if phase == 'serving' else 'work_running')
        elif a.get('slice_mode') and node_plan:
            self._set('slice_ready', None, self.progress)
        else:
            self._set('reported')
            self.detail = self._work_waiting(offer, run_id, bootstrap, native)

    def _descriptor_aging(self) -> bool:
        """True once less than half of this member's descriptor's life is left."""
        statement = (self._descriptor or {}).get('statement')
        issued = statement.get('issued_at') if isinstance(statement, Mapping) else None
        expires = statement.get('expires_at') if isinstance(statement, Mapping) else None
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (issued, expires)) or expires <= issued:
            return False  # no readable expiry: the owner's verification decides
        return self._clock() >= issued + (expires - issued) / 2

    def _work_waiting(self, offer: Mapping[str, Any] | None, run_id: str, bootstrap: str | None,
                      native: Mapping[str, Any]) -> str:
        """What this member is still waiting for in the work phase (a closed code)."""
        if native.get('state') == 'running':
            return 'bootstrap_control_running'
        if native.get('state') not in {None, 'stopped', 'closed', 'prepared'}:
            return _code(ValueError(native.get('error_code') or ''), 'native_worker_' + str(native.get('state')))
        if offer is None or offer.get('run_id') != run_id or offer.get('offer_id') == bootstrap:
            return 'work_offer_not_published'
        last = None
        standing = getattr(self.app, 'standing', None)
        if not self.local and standing is not None:
            try:
                last = standing.status().get('last')
            except Exception:
                last = None
        if (isinstance(last, Mapping) and last.get('offer_id') == offer.get('offer_id')
                and last.get('state') in {'refused', 'failed'} and last.get('reason')):
            http = last.get('error_http')
            self.error_http = dict(http) if isinstance(http, Mapping) else None
            return _code(ValueError(str(last['reason'])), 'work_not_accepted')
        return 'work_not_accepted'

    _ACQUIRING = frozenset({'starting', 'resolving_metadata', 'downloading', 'verifying', 'compiling'})
    _VERIFY_DETAIL = {'verifying': 'model_verifying', 'compiling': 'model_compiling'}

    def _ensure_whole_model(self, a: Mapping[str, Any]) -> bool:
        """Adopt the owner's announced model and fetch its whole snapshot here.

        Runs under the standing consent ('model_download') and within its storage
        cap; the app's own acquisition pins and verifies every byte.  Returns True
        once the announced model is prepared on this Mac.
        """
        model = a.get('model') or {}
        want = (model.get('model_id'), model.get('resolved_commit'))
        acquisition = self.app.acquisition
        status = acquisition.status()
        state = status.get('state')
        if state == 'prepared':
            self._retry_delay, self._retry_at = 0.0, None
            prepared = acquisition.prepared_source().model
            if (prepared.model_id, prepared.resolved_commit) != want:
                self._set('error', 'demo_model_mismatch')
                return False
            return True
        if state in self._ACQUIRING:
            # After the last byte the snapshot is verified, then compiled:
            # report that phase, so the owner does not judge a big model's
            # verification (bytes flat at the total) a stalled download.
            self._set('downloading_model', self._VERIFY_DETAIL.get(state),
                      {'done_bytes': int(status.get('downloaded_bytes') or 0),
                       'total_bytes': int(status.get('total_bytes') or 0)})
            return False
        if state == 'failed' and self._download_for == want:
            code = status.get('error_code') or 'model_download_failed'
            if code not in _DOWNLOAD_RETRYABLE:
                self._set('error', code)
                return False
            # A link drop exhausted every byte source (two-Mac run 21, phone
            # hotspot).  Back off, keep reporting the bytes already here, then
            # start again: the acquisition resumes its safe partials.  Bounded
            # by the owner's run: an idle/stopped announcement ends the retries.
            now = self._clock()
            if self._retry_at is None:
                self._retry_delay = min(_RETRY_MAX_SECONDS, self._retry_delay * 2) if self._retry_delay else \
                    _RETRY_FIRST_SECONDS
                self._retry_at = now + self._retry_delay
            if now < self._retry_at:
                self._set('downloading_model', 'model_download_retrying',
                          {'done_bytes': int(status.get('downloaded_bytes') or 0),
                           'total_bytes': int(status.get('total_bytes') or 0)})
                return False
            self._retry_at = None
        if not self._consent('model_download'):
            return False
        cap = int(self.consent.storage_cap(self.app))
        if not self._environment_selects(model):
            choice = model.get('choice')
            if not choice:
                self._set('error', 'demo_model_choice_unknown')
                return False
            self.app.action('/api/run-model', {'choice': choice})
        # Same consent and cap; only where the bytes come from (the owner's Mac
        # first, Hugging Face as the pinned fallback -- peer_model_source.py).
        setter = getattr(self.app.acquisition, 'set_byte_source', None)
        if setter is not None:
            setter(self._peer_source(a))
        self.app.action('/api/model/download', {'consent': {
            'download_acknowledged': True, 'max_download_bytes': cap, 'storage_limit_bytes': cap}})
        self._download_for = want
        self._set('downloading_model', None, {'done_bytes': 0, 'total_bytes': 0})
        return False

    def _environment_selects(self, model: Mapping[str, Any]) -> bool:
        """True when the environment already pins exactly the announced registered model.

        An environment-selected model (``MYCELIUM_A13_RUN_MODEL``) locks the
        picker and may have no picker choice (a9-route), so there is nothing to
        POST.  Any other case -- no lock, another revision, or a contract that
        differs from the registered one -- keeps the picker path and its refusals.
        """
        from .run_model import app_run_model
        from .serving import registered_model_contract
        try:
            _, source, current = app_run_model(self.app.root)
            registered = registered_model_contract(model.get('model_id'), model.get('resolved_commit'))
        except Exception:
            return False
        return source == 'environment' and current == registered

    def _peer_source(self, a: Mapping[str, Any]) -> Any:
        """The owner-served byte source for this run, or ``None`` (pinned origin only)."""
        if self._model_source is None:
            return None
        try:
            source = self._model_source(a)
        except Exception:
            source = None
        self._sources = source
        return source

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
        # A native process may have exited while its runtime is still attached.
        # Stop detaches it even when status is no longer `running`, before the
        # staging cleanup admission rejects an attached runtime.
        self.app.action('/api/control/stop', {})
        self.app.action('/api/staging/cleanup/accept', {'offer_id': requested['offer_id'],
            'archive_digest': requested['archive_digest'], 'cleanup_acknowledged': True})
        return True

    def _slice_reuse_binding(self, a, node_plan):
        import re
        key = a.get('artifact_reuse_key')
        if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key):
            return None
        try:
            member = self._actor().peer.staging_control_snapshot()
            return (key, node_plan['node_plan_digest'], a.get('owner_incarnation'),
                    tuple(member[k] for k in ('node_id', 'generation', 'incarnation',
                                             'verification_key_digest', 'endpoint_id')))
        except (AttributeError, KeyError, TypeError):
            return None

    def _move_cached_slice(self, root, binding):
        cache = getattr(self.app, '_demo_slice_cache', None) or {}
        previous = Path(cache['root']) if cache.get('root') else None
        if previous is None or previous == root:
            return
        base = self._cache_root() / 'slices'
        if previous.parent != base or previous.is_symlink() or previous.resolve() != previous.absolute():
            self.app._demo_slice_cache = None
            return
        if previous.is_dir():
            if binding is not None and cache.get('binding') == binding:
                # New descriptor root is still empty. rename keeps one copy and
                # the ordinary slice fetcher re-hashes every file before work.
                root.rmdir()
                previous.rename(root)
            else:
                shutil.rmtree(previous)
        self.app._demo_slice_cache = None

    def _drive_slice(self, a: Mapping[str, Any], node_plan: Mapping[str, Any]) -> None:
        run_id = a['run_id']
        root = self.slice_root(run_id)
        thread = self._slice_thread
        if thread is not None and thread.is_alive():
            self._set('downloading_slice', None, slice_progress(node_plan, root)); return
        if self._slice_result.get('failed_run_id') == run_id:
            self._set('error', self._slice_result.get('code'))
            self.error_http = self._slice_result.get('error_http')
            return
        if not self._consent('slice_fetch'):
            return
        cap = self.consent.storage_cap(self.app)
        share = a.get('member_resources')
        if isinstance(share, Mapping) and type(share.get('max_disk_bytes')) is int:
            cap = min(cap, share['max_disk_bytes'])  # its share's disk limit
        expected = node_plan['node_plan_digest']
        transport = self._slice_transport() if self._slice_transport else self._peer_source(a)
        if transport is None:
            from .acquisition import _HTTPRepositoryTransport
            transport = _HTTPRepositoryTransport()
        stop = self._slice_stop
        binding = self._slice_reuse_binding(a, node_plan)

        def work():
            from .slice_acquisition import fetch_node_slice
            try:
                self._move_cached_slice(root, binding)
                fetch_node_slice(node_plan, expected_node_plan_digest=expected, destination_root=root,
                                 transport=transport, max_download_bytes=cap, max_disk_bytes=cap,
                                 should_stop=stop.is_set)
                if binding is not None and not stop.is_set():
                    self.app._demo_slice_cache = {'root': str(root), 'binding': binding}
                self._slice_result = {'run_id': run_id}
            except Exception as error:
                code = _code(error, 'slice_fetch_failed')
                self._slice_result = {'failed_run_id': run_id, 'code': code,
                                      'error_http': _error_http(error, code)}

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
