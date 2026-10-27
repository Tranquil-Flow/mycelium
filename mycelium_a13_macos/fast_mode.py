# SPDX-License-Identifier: AGPL-3.0-only
"""Private next-run preference; explicit operator env wins over the GUI.

The standalone launcher owns a single app and binds its preference to a separate
process-default variable. Never rewrite MYCELIUM_TOKEN_PATH: it remains an
operator override. The default reaches all owner threads and inherited native
children (including frozen trampolines); it supplies no signed authority.
Embedded multi-app users must explicitly bind the app that owns the process.
"""
from contextlib import contextmanager
import os
from pathlib import Path

from .authority import _atomic_private_json, _load_private_json

PROTOCOL = 'mycelium.a13_fast_mode.v1'
FILE = 'fast-mode.json'
ENV = 'MYCELIUM_TOKEN_PATH'
APP_DEFAULT_ENV = 'MYCELIUM_TOKEN_PATH_APP_DEFAULT'


def document(root, *, environ=None):
    env = os.environ if environ is None else environ
    if ENV in env:
        value = env[ENV]
        if value not in ('on', 'off'):
            raise ValueError('token_path_activation_invalid')
        return {'enabled': value == 'on', 'source': 'environment', 'locked': True}
    try:
        saved = _load_private_json(Path(root) / FILE, max_bytes=4096)
        if saved is not None and (set(saved) != {'protocol', 'enabled'}
                or saved['protocol'] != PROTOCOL or type(saved['enabled']) is not bool):
            raise ValueError('fast_mode_state_invalid')
    except Exception:
        raise ValueError('fast_mode_state_invalid') from None
    return {'enabled': False if saved is None else saved['enabled'],
            'source': 'default' if saved is None else 'saved', 'locked': False}


def run_active(app):
    run = getattr(app, 'owner_run', None)
    if run is not None and run.active():
        return True
    # The advanced serving API can prepare/start a route without OwnerRun.
    for name in ('_chain_compile_thread', '_chain_serving_thread'):
        thread = getattr(app, name, None)
        if thread is not None and thread.is_alive():
            return True
    workflow = getattr(app, '_chain_workflow', None)
    return workflow is not None and workflow.status().get('state') not in {'stopped', 'failed'}


def choose(app, body):
    """Called under app.guard, the same fence as Start and Rebuild."""
    from .app import DemoError
    if app.installation_role != 'owner':
        raise DemoError('owner_role_required')
    if not isinstance(body, dict) or set(body) != {'enabled'} or type(body['enabled']) is not bool:
        raise DemoError('invalid_request')
    if run_active(app):
        raise DemoError('fast_mode_run_active')
    if document(app.root)['locked']:
        raise DemoError('fast_mode_locked_by_environment')
    _atomic_private_json(Path(app.root) / FILE,
                         {'protocol': PROTOCOL, 'enabled': body['enabled']}, max_bytes=4096)
    refresh = getattr(app, '_refresh_fast_mode_process', None)
    if refresh is not None:
        refresh()
    return document(app.root)


@contextmanager
def bind_owner_process(app):
    """Only the dedicated app launcher binds global process defaults; restore on exit."""
    if getattr(app, '_refresh_fast_mode_process', None) is not None:
        raise ValueError('fast_mode_process_already_bound')
    previous = os.environ.get(APP_DEFAULT_ENV)

    def refresh():
        enabled = app.installation_role == 'owner' and document(app.root)['enabled']
        os.environ[APP_DEFAULT_ENV] = 'on' if enabled else 'off'

    try:
        refresh()
        app._refresh_fast_mode_process = refresh
        yield
    finally:
        app._refresh_fast_mode_process = None
        if previous is None:
            os.environ.pop(APP_DEFAULT_ENV, None)
        else:
            os.environ[APP_DEFAULT_ENV] = previous
