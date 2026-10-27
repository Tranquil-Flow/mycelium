# SPDX-License-Identifier: AGPL-3.0-only
"""Published UI observations, isolated from app/authority/network work.

One app-owned maintenance thread samples the legacy aggregate (which also
performs existing monitor/refresh work). HTTP readers only copy its last
completed publication and overlay cheap display state. Successful actions
publish before returning; status readers never run maintenance or wait for
app/authority/network work.
Snapshots are display information, NEVER admission or ownership evidence.
"""
from __future__ import annotations

import copy
import threading
import time


class PublishedStatus:
    def __init__(self, app):
        self.app = app
        self._published = None
        self._failed = False
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name='a13-status-publisher', daemon=True)

    def start(self):
        # Before the HTTP accept loop and standing/policy workers start: publish
        # a complete initial shape, not a first-request cache miss under compile.
        self.refresh()
        self.thread.start()

    def refresh(self):
        app = self.app
        if self._stop.is_set() or app.closed.is_set():
            return
        # Monitoring can tick a contributor (agent lock -> app.guard). Never
        # run it under app.guard: the inverse order deadlocks a concurrent tick.
        # It belongs to maintenance, not to the snapshot or a reader.
        try:
            run = app.owner_run
            if run is not None:
                run.status()
            from .sharing_state import sharing_state
            sharing_state(app)  # retain the existing rejoin-latch maintenance
        except Exception:
            self._failed = True
            return
        self.publish()

    def publish(self):
        """Collect completed actions, without ticking monitors or waiting on work.

        The guard serializes publications as well as actions: an older collector
        cannot overwrite a newer completed action. A busy long operation keeps
        the last complete view; cheap saved settings are overlaid by read().
        """
        app = self.app
        if self._stop.is_set() or app.closed.is_set() or not app.guard.acquire(blocking=False):
            return
        try:
            if self._stop.is_set() or app.closed.is_set():
                return
            value = copy.deepcopy(app.status())
            self._published = (time.monotonic(), value)
            self._failed = False
        except Exception:
            # Preserve the last complete observation, never a partial aggregate.
            self._failed = True
        finally:
            app.guard.release()

    def _loop(self):
        while not self._stop.wait(1.0):
            if self.app.closed.is_set():
                return
            self.refresh()

    def stop(self):
        self._stop.set()
        if self.thread.ident is not None and self.thread is not threading.current_thread():
            self.thread.join(2)

    def read(self):
        from .app import DemoError
        app = self.app
        if app.closed.is_set():
            raise DemoError('application_closed')
        publication = self._published
        if publication is None:
            raise DemoError('status_unavailable')
        sampled, base = publication
        value = copy.deepcopy(base)
        role = app.installation_role
        if role != value['installation_role']:
            # A role transition cannot expose the former owner's observation as
            # a member's authority. The next completed refresh fills these in.
            value.update(authority={}, join_flow={}, leave=None, demo_target=None,
                         owner_run=None, chat=None, swarm=None, relay_wait=None)
        value['installation_role'] = role
        value['running'] = app.running
        # Small atomically replaced settings and in-memory policy observations
        # remain current even when a compiler holds the aggregate's guard.
        from .fast_mode import document as fast_mode_document
        from .run_model import document as run_model_document
        from .app import read_resource_policy
        value['fast_mode'] = fast_mode_document(app.root)
        value['run_model'] = run_model_document(app.root)
        from dataclasses import asdict
        value['model_contract'] = asdict(app.run_model_contract())
        value['resource_policy'] = read_resource_policy(app.root)
        value['resource_observation'] = dict(app.resource_observation)
        value['acquisition_policy_error'] = app.acquisition_policy_error
        # Remote join/resume changes the seed roster without a local UI action.
        # members() projects only in-memory records. Try its RLock first so a
        # seed transaction can never make a display request wait on durable I/O.
        coordinator = getattr(app.authority, 'coordinator', None) if role == 'owner' else None
        lock = getattr(coordinator, '_lock', None)
        if lock is not None and lock.acquire(blocking=False):
            try:
                clocks = {m['node_id']: m.get('clock') for m in value['authority'].get('members', [])}
                members = coordinator.members()
                value['authority']['members'] = [
                    {'node_id': m['node_id'], 'generation': m['generation'],
                     'lifecycle_state': m['lifecycle_state'], 'clock': clocks.get(m['node_id'])}
                    for m in members]
                value['authority']['member_count'] = len(members)
            finally:
                lock.release()
        run = app.owner_run if role == 'owner' else None
        from .demo_app import owner_status
        value['owner_run'] = owner_status(app, observe_only=True)
        owner = value.get('owner_run') or {}
        agent = (getattr(getattr(run, 'chain', None), 'agent', None)
                 if role == 'owner' else app.demo_target)
        if agent is not None:
            value['demo_target'] = agent.status_snapshot()
            if (role == 'target' and value['demo_target'].get('state') == 'idle'
                    and value['authority'].get('membership_state') == 'current'):
                value['demo_target']['state'] = 'joined'
        chat = value.get('chat')
        if role == 'owner' and isinstance(chat, dict):
            chat['available'] = owner.get('state') == 'serving' and bool(owner.get('registry_origin'))
            chat['busy'] = app.chat is not None and app.chat._busy is not None
        value['serving'] = app._serving_block(owner, relay_wait=value.get('relay_wait'))
        value['status_snapshot'] = {
            'age_seconds': round(max(0.0, time.monotonic() - sampled), 3),
            'refresh_failed': self._failed,
        }
        return value
