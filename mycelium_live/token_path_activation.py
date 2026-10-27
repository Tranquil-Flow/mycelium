# SPDX-License-Identifier: AGPL-3.0-only
"""Local operator opt-in, not grant authority or capability negotiation.

Only exact on/off values are accepted. A private host must still provision the
owner, native participant policies and target receipt cursor independently.
"""
import os


def token_path_enabled():
    # The GUI launcher's process default is inherited by native children.
    # An explicit operator setting always wins, including explicit OFF.
    value = os.environ.get('MYCELIUM_TOKEN_PATH',
                           os.environ.get('MYCELIUM_TOKEN_PATH_APP_DEFAULT', 'off'))
    if value not in ('off', 'on'):
        raise ValueError('token_path_activation_invalid')
    return value == 'on'


def require_token_path_owner(owner):
    """Refuse an ON launch without wiring, before any route/device side effect."""
    if token_path_enabled() and (owner is None or getattr(owner, 'enabled', None) is not True):
        raise ValueError('token_path_activation_unprovisioned')
