# SPDX-License-Identifier: AGPL-3.0-only
"""The activation barrier, driven through the PINNED step's real `main()` against a scripted app.

WHY THIS IS A SEQUENCING DEFECT, NOT A TIMEOUT. The app's own order is (workflow.py:206-236):

    _state='publishing' -> publish_controller_offers(...) -> _state='staging'
    -> controller.execute('prepare')            <-- the target-owned prepare, tens of seconds
    -> (activation_event) _state='awaiting_activation'

The offers therefore EXIST while the app still reports `staging`, and `awaiting_activation` is reached only
after `prepare` finishes. The pinned step previews and accepts the moment an offer appears and then calls
activate immediately (`round14_start_and_activate.py:26-41`), so it can legitimately ask for activation
while the app is still `staging` -- and the app's own handler then refuses, in its own words
(chain_compile.py:116):

    if workflow is None or gate is None or workflow.status()['state']!='awaiting_activation':
        raise DemoError('managed_workflow_not_awaiting_activation')

The round dies on that refusal. Nothing about it is a timeout, and no wider window would have helped: the
round asked too early. The fix is to wait on the app's own `serving.state` report.

The scripted app below enforces the SAME preconditions the real handlers do and refuses in the app's own
vocabulary (`local_api_http_409:<code>`, the shape `app_rpc.AppClient.call` produces), so both verdicts
read as the real round's would. The RED witness drives the PINNED step unchanged with the readiness gate
disabled -- the pre-fix transport -- and the app refuses.
"""
import importlib
import pathlib
import sys
import threading

import pytest

OPERATOR = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator'


class ServingApp:
    """The app's own /api/serving/* state machine, scripted and deterministic.

    * `compilation` is consumed by each `/api/serving/status` read (the last entry repeats);
    * `serving` is None until `start` -- the app reports `None if workflow is None` (app.py:445) -- then
      follows the app's own order: staging -> awaiting_activation -> starting -> api_running;
    * a control call is refused unless the app's own handler's precondition holds, reported by the app.
    """

    def __init__(self, *, compilation=('compiling',), awaiting_after=2):
        self.compilation_schedule = list(compilation)
        self.awaiting_after = awaiting_after
        self.serving = None
        self.staging_reads = 0
        self.activations = 0
        self.calls = []
        self._lock = threading.Lock()

    # -- the app's own reports -------------------------------------------------------------------
    def _compilation(self):
        if len(self.compilation_schedule) > 1:
            return self.compilation_schedule.pop(0)
        return self.compilation_schedule[0]

    def _report(self):
        with self._lock:
            self.calls.append('/api/serving/status')
            compilation = self._compilation()
            if self.serving == 'staging':
                self.staging_reads += 1
                if self.staging_reads >= self.awaiting_after:
                    self.serving = 'awaiting_activation'
            elif self.serving == 'starting':
                self.serving = 'api_running'
            state = self.serving
        compilation_report = {'state': compilation}
        if compilation == 'compiled':
            # chain_compile.py:69 -- the compiled report carries the exact execution proposal.
            compilation_report['execution_proposal'] = {'max_wall_seconds': 1800, 'run_count': 1,
                                                        'max_combined_rss_bytes': 16_955_405_108,
                                                        'max_additional_disk_bytes': 24_854_140_006}
        return {'compilation': compilation_report,
                'serving': None if state is None else {'state': state}}

    def _refuse(self, code):
        raise RuntimeError('local_api_http_409:%s' % code)

    # -- the app's own control surface -----------------------------------------------------------
    def call(self, path, body=None):
        with self._lock:
            if path != '/api/serving/status':
                self.calls.append(path)
            state = self.serving
        if path == '/api/serving/status':
            return self._report()
        if path == '/api/serving/start':
            if self._compilation() != 'compiled':
                self._refuse('model_execution_proposal_changed')
            self.serving = 'staging'
            return {'state': 'starting', 'registry_origin': 'http://127.0.0.1:62346/', 'route_ready': False}
        if path == '/api/serving/activate':
            if state != 'awaiting_activation':
                self._refuse('managed_workflow_not_awaiting_activation')
            self.serving = 'starting'
            self.activations += 1
            return {'state': 'activation_requested', 'route_ready': False}
        if path in ('/api/staging/check', '/api/staging/cleanup/check'):
            published = state is not None
            return {'offer': {'offer_id': 'offer-1', 'archive_digest': 'sha256:' + 'ab' * 32} if published else None}
        if path == '/api/work/preview':
            if state is None:
                self._refuse('work_offer_missing')
            return {'consent_digest': 'sha256:' + 'cd' * 32}
        if path == '/api/work/accept':
            return {'state': 'running', 'error_code': None, 'route_ready': False}
        if path == '/api/status':
            return {'state': 'ok'}
        return {'ok': path}

    def client_factory(self, root):
        """Stand in for `_client(root)`."""
        return self

    def once(self, path, body=None):
        """Stand in for the remote `_target_once(path, body)` ssh hop."""
        return self.call(path, body)


@pytest.fixture()
def transport(tmp_path, monkeypatch):
    """Load `round14_rpc` exactly as the pinned step does (same import name, same module object)."""
    monkeypatch.setenv('A13_OWNER_ROOT', str(tmp_path / 'owner'))
    monkeypatch.setenv('A13_REMOTE_ROOT', str(tmp_path / 'remote'))
    monkeypatch.setenv('A13_TARGET_PREVIEW', 'preview82')
    monkeypatch.setenv('A13_RPC_ROUND', 'rpc-integration')
    monkeypatch.syspath_prepend(str(OPERATOR))
    for name in ('round14_rpc', 'round14_start_and_activate'):
        sys.modules.pop(name, None)
    module = importlib.import_module('round14_rpc')
    monkeypatch.setattr(module.time, 'sleep', lambda s: None)
    yield module
    for name in ('round14_rpc', 'round14_start_and_activate'):
        sys.modules.pop(name, None)


def _drive_the_pinned_step(monkeypatch, rpc, owner_app, target_app):
    """Run the PINNED `round14_start_and_activate.main()` against the scripted apps.

    Only the transport seam is scripted (`_client`, the remote ssh hop, the surface checks); every
    sequencing decision -- including the readiness gate -- is the code under test.
    """
    rpc._OWNER_ANSWERED.clear()
    rpc._AWAKE.clear()
    rpc._TARGET_SURFACE_CHECKED.clear()
    monkeypatch.setattr(rpc, 'require_local', lambda root: None)
    monkeypatch.setattr(rpc, 'require_remote', lambda *args, **kwargs: None)
    monkeypatch.setattr(rpc, '_client', owner_app.client_factory)
    monkeypatch.setattr(rpc, '_target_once', target_app.once)
    step = importlib.import_module('round14_start_and_activate')
    step.main()
    return step


def test_the_round_does_not_ask_for_activation_before_the_app_is_awaiting_it(monkeypatch, transport):
    """GREEN: preview/accept on the app's own offer report, then activate only once the app is awaiting."""
    owner_app = ServingApp(compilation=('compiling', 'compiling', 'compiled'), awaiting_after=2)
    target_app = ServingApp(compilation=('compiled',))
    target_app.serving = 'staging'          # its offer is published; the target side is not gated

    _drive_the_pinned_step(monkeypatch, transport, owner_app, target_app)

    assert owner_app.activations == 1
    activate_at = owner_app.calls.index('/api/serving/activate')
    reads_before = owner_app.calls[:activate_at].count('/api/serving/status')
    assert reads_before >= 4, (
        'the app had to report readiness (%d status reads before activation)' % reads_before)


def test_the_pre_fix_sequencing_is_refused_by_the_app_in_its_own_words(monkeypatch, transport):
    """RED witness: the SAME pinned step, with the readiness gate disabled -- the pre-fix transport.

    The round accepts the offers while the app is `staging` (they are published then, workflow.py:217-221)
    and activates immediately; the app refuses exactly as it does in a round. Kept as a witness so the
    sequencing that was removed cannot return unnoticed.
    """
    owner_app = ServingApp(compilation=('compiling', 'compiling', 'compiled'), awaiting_after=2)
    target_app = ServingApp(compilation=('compiled',))
    target_app.serving = 'staging'

    monkeypatch.setattr(transport, 'await_ready', lambda root, path: None, raising=False)   # pre-fix

    with pytest.raises(RuntimeError) as excinfo:
        _drive_the_pinned_step(monkeypatch, transport, owner_app, target_app)

    assert 'managed_workflow_not_awaiting_activation' in str(excinfo.value)
    assert owner_app.activations == 0, 'the app never activated'
