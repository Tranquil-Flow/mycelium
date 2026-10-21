# SPDX-License-Identifier: AGPL-3.0-only
"""Exact-run native session factory for enrolled managed-HTTPS participants.

Matches the canonical controller/live-route factory protocol. It never executes
argv, provisions files or turns an unadmitted node name into a target identity.
"""
from __future__ import annotations
import json
import math
from .control_session import ManagedControlOwner, MAX_SESSION_TIMEOUT_SECONDS
from .managed_control import _binding_from_member


class ManagedSessionFactoryError(ValueError):
    pass


class ManagedSessionFactory:
    def __init__(self, *, owner:ManagedControlOwner, run_id:str, deployment_id:str,
                 members):
        if not isinstance(owner,ManagedControlOwner):
            raise ManagedSessionFactoryError('managed_session_factory_invalid')
        self.owner=owner
        self.run_id=run_id;self.deployment_id=deployment_id
        self._bindings={}
        try:
            for member in members:
                value=json.loads(json.dumps(dict(member),allow_nan=False))
                binding=_binding_from_member(value,run_id,deployment_id)
                node=binding['target_node_id']
                if node in self._bindings:raise ValueError()
                self._bindings[node]=binding
            if not self._bindings:raise ValueError()
        except Exception:
            raise ManagedSessionFactoryError('managed_session_factory_members_invalid') from None

    def __call__(self, *, argv, node_id, run_id, deployment_id, timeout_seconds):
        if (argv!=() or not isinstance(node_id,str) or node_id not in self._bindings
            or run_id!=self.run_id or deployment_id!=self.deployment_id
            or type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds)
            or timeout_seconds<=0):
            raise ManagedSessionFactoryError('managed_session_factory_scope_mismatch')
        try:
            session=self.owner.session(node_id=node_id,run_id=run_id,
                deployment_id=deployment_id,timeout_seconds=min(timeout_seconds,MAX_SESSION_TIMEOUT_SECONDS))
            if session.binding!=self._bindings[node_id]:raise ValueError()
            return session
        except Exception:
            raise ManagedSessionFactoryError('managed_session_factory_member_not_current') from None
