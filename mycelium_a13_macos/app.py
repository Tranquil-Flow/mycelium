# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private, loopback-only macOS demo application. Never a public admin server."""
from __future__ import annotations

try:  # instance lock; win32 goes through mycelium_host_platform
    import fcntl
except ImportError:  # pragma: no cover - win32
    fcntl = None
import hashlib
import html
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import stat
import threading
import time
import urllib.parse
from dataclasses import asdict
import mycelium_host_platform
from .serving import RESOURCE_PROPOSAL

MAX_BODY = 65536
# Refusal for a request that cannot be served because every concurrency slot is held. It is a
# fixed literal, so no path, credential, traceback or internal state can appear in it.
BUSY_BODY = b'{"error":"server_busy"}'
BUSY_RESPONSE = (b'HTTP/1.1 503 Service Unavailable\r\n'
                 b'Content-Type: application/json\r\n'
                 b'Content-Length: ' + str(len(BUSY_BODY)).encode('ascii') + b'\r\n'
                 b'Connection: close\r\n'
                 b'Cache-Control: no-store\r\n'
                 b'Retry-After: 1\r\n'
                 b'\r\n' + BUSY_BODY)


def model_contract():
    from .serving import selected_run_model
    return asdict(selected_run_model())


class DemoError(RuntimeError):
    pass


def _admitted_macos(os_version):
    if os_version == '15.6.1':
        return True
    parts = os_version.split('.') if isinstance(os_version, str) else []
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() and len(p) <= 3 for p in parts):
        return False
    return int(parts[0]) >= 26


def preflight(*, system=None, machine=None, os_version=None):
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    if system == 'Windows':
        return _windows_preflight(machine=machine, os_version=os_version)
    if system == 'Linux':
        return _linux_preflight(machine=machine, os_version=os_version)
    os_version = platform.mac_ver()[0] if os_version is None else os_version
    # 15.6.1 stays an exact historical target; any macOS 26 or later release is
    # admitted because that is the bundled runtime's own floor (Homebrew
    # Python/OpenSSL built for 26.0). Admitting a release here is not evidence
    # that the final package or physical serving was exercised on it.
    if system != 'Darwin' or machine != 'arm64' or not _admitted_macos(os_version):
        error=DemoError('unsupported_platform')
        error.observed_platform={'system':system,'machine':machine,'os_version':os_version}
        raise error
    return {'platform': 'macos', 'architecture': machine, 'os_version': os_version,
            'model_contract':model_contract(),
            'distribution': 'unsigned_development_demo'}


#: Windows owner/target app floor (full-parity ruling): the A13 Windows platform
#: admission's declared minimum build on x86_64 (Windows 10 22H2, build 19045).
def _windows_min_build():
    from mycelium_a13_windows.platform_admission import ADMITTED_RELEASES
    return int(ADMITTED_RELEASES['windows']['minimum_build'])


def _windows_preflight(*, machine, os_version):
    version = platform.version() if os_version is None else os_version
    try:
        build = int(str(version).split('.')[2])
    except (IndexError, ValueError):
        build = 0
    if machine not in {'AMD64', 'x86_64'} or build < _windows_min_build():
        error=DemoError('unsupported_platform')
        error.observed_platform={'system':'Windows','machine':machine,'os_version':version}
        raise error
    return {'platform': 'windows', 'architecture': 'x86_64', 'os_version': version,
            'model_contract':model_contract(),
            'distribution': 'unsigned_development_demo'}


def _linux_preflight(*, machine, os_version):
    """Linux floor: the A13 Linux platform admission (Ubuntu 24.04+, x86_64/aarch64).

    ``os_version`` is ``<distribution_id>-<version>`` (e.g. ``ubuntu-24.04``);
    by default it is read from /etc/os-release.
    """
    from mycelium_a13_linux.platform_admission import ADMITTED_ARCHITECTURES, ADMITTED_DISTRIBUTIONS
    if os_version is None:
        from mycelium_a13_linux.installer import read_os_release
        release = read_os_release()
        os_version = f"{release.get('ID', '')}-{release.get('VERSION_ID', '')}"
    distribution, _, version = str(os_version).partition('-')
    declared = ADMITTED_DISTRIBUTIONS.get(distribution)
    try:
        admitted = declared is not None and tuple(int(p) for p in version.split('.')) >= tuple(
            int(p) for p in declared['minimum_version'].split('.'))
    except ValueError:
        admitted = False
    if machine not in ADMITTED_ARCHITECTURES or not admitted:
        error=DemoError('unsupported_platform')
        error.observed_platform={'system':'Linux','machine':machine,'os_version':os_version}
        raise error
    return {'platform': 'linux', 'architecture': machine, 'os_version': os_version,
            'model_contract':model_contract(),
            'distribution': 'unsigned_development_demo'}


def _win32_private_file(path, *, max_bytes):
    """Owner-only, single-link, reparse-refusing read; None when absent (win32)."""
    try:
        with mycelium_host_platform.private_directory(Path(path).parent, create=False) as lease:
            return lease.read_file(Path(path).name, max_bytes=max_bytes)
    except mycelium_host_platform.HostPlatformError as exc:
        if exc.code == 'host_file_missing':
            return None
        raise DemoError('unsafe_state_permissions') from exc


def private_dir(path):
    path = Path(path).absolute()
    if path.is_symlink() or path.resolve() != path:
        raise DemoError('unsafe_state_path')
    if mycelium_host_platform.is_win32():
        try:
            with mycelium_host_platform.private_directory(path, create=True):
                pass
        except mycelium_host_platform.HostPlatformError as exc:
            raise DemoError('unsafe_state_permissions') from exc
        return path
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise DemoError('unsafe_state_permissions')
    return path


def private_write(path, raw):
    if mycelium_host_platform.is_win32():
        try:
            with mycelium_host_platform.private_directory(Path(path).parent, create=False) as lease:
                lease.atomic_replace(Path(path).name, raw)
        except mycelium_host_platform.HostPlatformError as exc:
            raise DemoError('unsafe_state_permissions') from exc
        return
    temp = path.with_name('.'+path.name+'.'+secrets.token_hex(8))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        temp.unlink(missing_ok=True)


def consent_valid(c):
    # G2 (D-1, 2026-09-16): the storage ceiling is derived from the selected
    # contract's own resource proposal (serving.RESOURCE_PROPOSAL.
    # additional_disk_bytes = the largest per-host requirement: the laptop's
    # seeded 7B snapshot 15,242,807,270 B + its [0,14)+embedding stage copy
    # 7,810,167,792 B = 23,052,975,062 B).  It must be expressible, because the
    # seeded-path policy also bounds the acquisition's storage grant and the
    # worker's staging disk.  See runs/wp8-02-g1/.
    return (isinstance(c, dict) and set(c) == {'peer_visibility','power','storage_bytes','network','background','diagnostics','thermal'}
            and c['peer_visibility'] is True and c['power'] in {'ac_only','battery_allowed'}
            and type(c['storage_bytes']) is int and 100_000_000 <= c['storage_bytes'] <= RESOURCE_PROPOSAL.additional_disk_bytes
            and c['network'] in {'unmetered_only','metered_allowed'}
            and c['background'] is False and c['diagnostics'] is False
            and c['thermal'] == 'stop_on_serious')


def read_resource_policy(root):
    path=root/'resource-policy.json'
    if mycelium_host_platform.is_win32():
        raw=_win32_private_file(path,max_bytes=4096)
        if raw is None: return None
        value=json.loads(raw)
        if not consent_valid(value): raise DemoError('resource_policy_invalid')
        return value
    try: fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError: return None
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid()
            or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>4096):
            raise DemoError('resource_policy_invalid')
        value=json.loads(os.read(fd,4097))
        if not consent_valid(value): raise DemoError('resource_policy_invalid')
        return value
    finally: os.close(fd)


def reopen_native(root):
    """Explicit native-owner reauthorization; never an unauthenticated web grant."""
    root = private_dir(Path(root).expanduser())
    if mycelium_host_platform.is_win32():
        raw = _win32_private_file(root / 'native-control.json', max_bytes=4096)
        if raw is None: raise DemoError('native_control_unavailable')
        control = json.loads(raw)
    else:
        fd = os.open(root / 'native-control.json', os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                raise DemoError('native_control_unavailable')
            control = json.loads(os.read(fd,4097))
        finally: os.close(fd)
    if set(control) != {'port','credential'} or type(control['port']) is not int or not 1 <= control['port'] <= 65535:
        raise DemoError('native_control_unavailable')
    connection = http.client.HTTPConnection('127.0.0.1',control['port'],timeout=3)
    try:
        connection.request('POST','/native-session','{}',headers={
            'Content-Type':'application/json','X-Native-Authorization':control['credential']})
        response=connection.getresponse(); response.read(4096)
        if response.status != 200: raise DemoError('native_control_unavailable')
    finally: connection.close()
    return root / 'open-demo.html'


def bounded_failure_locus(error,state_root):
    """Root-relative, bounded identity for an OS-level startup failure.

    A bare `FileNotFoundError` during launch was unattributable (rounds 51).
    """
    from pathlib import Path
    name=getattr(error,'filename',None)
    if not isinstance(name,str) or not name:return {}
    try:relative=Path(name).resolve().relative_to(Path(state_root).resolve())
    except (OSError,ValueError):return {'locus':'outside_state_root'}
    text='/'+relative.as_posix()
    return {'locus':text if len(text)<=160 else text[:157]+'...'}


def public_failure_code(error):
    """Only library-owned closed error codes; never exception strings."""
    import re
    code=getattr(error,'code',None)
    if (type(error).__module__.startswith('mycelium_') and isinstance(code,str)
        and re.fullmatch(r'[a-z][a-z0-9_]{0,95}',code)):
        return code
    return 'action_not_completed'


def bounded_action_failure(path, error, public_code):
    """Private bounded failure identity; never raw exception messages.

    A collapsed public code (`action_not_completed` / `managed_control_unreachable`)
    cost a whole debugging cycle: the operator is told the generic symptom and the
    one fact that would name the cause -- the exact request path and inner error
    code -- is discarded.  Keep the allowlisted class/module/status/path only; no
    message text, no headers, no credentials.
    """
    import re
    request_path = getattr(error, 'request_path', None)
    status = getattr(error, 'status', None)
    code = getattr(error, 'code', None)
    cause_code = getattr(error, 'cause_code', None)
    record = {
        'protocol': 'mycelium.a13.action_failure.v1',
        'path': path if isinstance(path, str) and re.fullmatch(r'/[A-Za-z0-9_./-]{0,160}', path) else '/invalid',
        'public_code': public_code if isinstance(public_code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', public_code) else 'action_not_completed',
        'exception_type': type(error).__name__[:64],
        'exception_module': type(error).__module__[:96],
    }  # type: dict[str, object]
    if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', code):
        record['error_code'] = code
    if isinstance(cause_code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', cause_code):
        record['cause_code'] = cause_code
    if type(status) is int and 100 <= status <= 599:
        record['status'] = status
    if isinstance(request_path, str) and re.fullmatch(r'/[A-Za-z0-9_./-]{0,160}', request_path):
        record['request_path'] = request_path
    return record


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8
    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)
    def answer_busy(self, request):
        # Every slot is held: say so explicitly instead of closing with no response at all.
        # The write must never reach (or stop) the accept loop, and no handler exists yet, so
        # nothing but the fixed BUSY_RESPONSE bytes can reach the client.
        try: request.sendall(BUSY_RESPONSE)
        except Exception: pass
        # Read what the client already sent -- bounded, never blocking -- so closing this socket
        # is a FIN rather than a reset that could destroy the refusal already in flight.
        try:
            for _ in range(8):
                if not request.recv(65536, socket.MSG_DONTWAIT): break
        except Exception: pass
    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.answer_busy(request); self.shutdown_request(request); return
        try: super().process_request(request, client_address)
        except BaseException:
            self.slots.release(); raise
    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally: self.slots.release()
    def handle_error(self, request, client_address):
        # HTTP errors must not expose bodies, credentials, private paths or tracebacks.
        pass


class DemoApp:
    def __init__(self, state_root, *, authority_factory=None, check_platform=True, _test_peer_factory=None):
        self.root = Path(state_root).expanduser().absolute()
        self.check_platform = check_platform
        self.authority_factory = authority_factory
        self._test_peer_factory = _test_peer_factory
        self.installation_role = 'owner'
        self.authority = None
        self.server = None
        self.thread = None
        self.lock_fd = None
        self.guard = threading.RLock()
        self.closed = threading.Event()
        self.running = False
        self.renew_error = None
        self.launch_file = self.root / 'open-demo.html'
        self.session = None
        self.session_expires = 0
        self.launch_code = None
        self.launch_expires = 0
        self.native_credential = secrets.token_urlsafe(32)
        self.cleanup_error = None
        self.acquisition = None
        self.public_relay = None
        self.recovery_error = None
        self.local_contributor = None
        self.acquisition_policy_error = None
        self._acquisition_active = False
        from .resources import observe_policy, UNKNOWN_OBSERVATION
        self._policy_observer = observe_policy
        self.resource_observation = dict(UNKNOWN_OBSERVATION)

    @property
    def port(self): return self.server.server_address[1]
    @property
    def origin(self): return f'http://127.0.0.1:{self.port}'

    def start(self):
        if self.check_platform: preflight()
        private_dir(self.root)
        if mycelium_host_platform.is_win32():
            try:
                self._win32_instance_lock = mycelium_host_platform.hold_exclusive_lock(
                    self.root, 'application.lock', blocking=False)
            except mycelium_host_platform.HostPlatformError as exc:
                raise DemoError('already_running' if exc.code == 'host_lock_busy' else 'unsafe_lock_file') from None
        else:
            self.lock_fd = os.open(self.root / 'application.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info=os.fstat(self.lock_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid()
                or stat.S_IMODE(info.st_mode)!=0o600):
                os.close(self.lock_fd); self.lock_fd=None
                raise DemoError('unsafe_lock_file')
            try: fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(self.lock_fd); self.lock_fd = None
                raise DemoError('already_running') from None
        try:
            if self.authority_factory is None:
                from .authority import DemoAuthority
                self.authority_factory = DemoAuthority
            from .authority import _load_private_json
            selection = _load_private_json(self.root / 'installation-role.json')
            if selection is not None:
                if selection != {'protocol':'mycelium.a13_installation_role.v1','role':'target'}:
                    raise DemoError('installation_role_invalid')
                owner_root = self.root / 'authority'
                if owner_root.exists() and any(owner_root.iterdir()):
                    raise DemoError('installation_role_conflict')
                if read_resource_policy(self.root) is None:
                    raise DemoError('target_consent_missing')
                from .target_app import TargetApplication
                self.installation_role = 'target'
                self.authority = TargetApplication(self.root / 'target', _test_peer_factory=self._test_peer_factory)
            else:
                if (self.root / 'target').exists():
                    raise DemoError('installation_role_missing')
                self.authority = self.authority_factory(self.root / 'authority')
            try:
                self.authority.start()
                self.running = True
            except Exception as exc:
                from .peer import RemoteMacMemberError
                if (self.installation_role != 'target' or not isinstance(exc, RemoteMacMemberError)
                    or exc.code not in {'peer_transport_unreachable', 'seed_http_remote_error'}):
                    raise
                # A dead relay must not remove the local authenticated recovery
                # surface. No member, native worker or contribution is enabled.
                self.recovery_error = exc.code
                self.running = False
            self._open_acquisition()
            self.server = _Server(('127.0.0.1', 0), _Handler)
            self.server.app = self
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            private_write(self.root / 'native-control.json',json.dumps({
                'port':self.port,'credential':self.native_credential}).encode())
            self.make_launch_file()
            self.renew_thread = threading.Thread(target=self._renew_loop, daemon=True)
            self.renew_thread.start()
            self.policy_thread = threading.Thread(target=self._policy_loop, daemon=True)
            self.policy_thread.start()
            return self
        except BaseException:
            self.close(); raise

    def _open_acquisition(self):
        from .acquisition import SelectedModelAcquisition
        if self.acquisition is not None:
            self.acquisition.close()
            self.acquisition = None
        # A target's downloaded weights must be inside the same private cache
        # that its staging consumer confines and re-verifies. Retain old owner
        # cache bytes on role change; never migrate or delete unrelated state.
        from .serving import CANDIDATE_MODEL as _CANDIDATE, selected_run_model
        model = selected_run_model()
        # A non-default run model keeps its own cache and status, never the candidate's.
        suffix = '' if model == _CANDIDATE else '-' + model.resolved_commit[:12]
        # A target's cache stays the staging consumer's confined 'serving' root.
        root = (self.authority.peer.state_root / 'serving'
                if self.installation_role == 'target' else self.root / ('model-source' + suffix))
        self.acquisition = SelectedModelAcquisition(root, model=model)
        self._acquisition_active = False

    def make_launch_file(self):
        self.launch_code = secrets.token_urlsafe(32)
        self.launch_expires = time.monotonic() + 120
        # Local one-use POST grants this browser; no credential in any URL.
        raw = ('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
               '<title>Open macOS A13 demo</title><h1>Open your private demo</h1>'
               '<p>This local file authorizes one browser session. It expires in two minutes.</p>'
               f'<form method="post" action="{self.origin}/session">'
               f'<input type="hidden" name="launch" value="{self.launch_code}">'
               '<button type="submit">Open private demo</button></form>').encode()
        private_write(self.launch_file, raw)

    def _start_local_contributor(self):
        if self.local_contributor is not None:raise DemoError('local_contributor_already_running')
        from .local_contributor import create_local_contributor
        self.local_contributor=create_local_contributor(self)
        return {'created':True,'physical_hosts':1,'node_id':self.local_contributor.member.node_id,
                'membership_state':'current','route_ready':False}

    def _stop_local_contributor(self):
        if self.local_contributor is not None:
            self.local_contributor.stop()
            self.local_contributor=None

    def _renew_loop(self):
        while not self.closed.wait(20):
            with self.guard:
                if self.closed.is_set() or not self.running: continue
                try:
                    self.authority.renew()
                    if self.local_contributor is not None:self.local_contributor.renew()
                    self.renew_error = None
                except Exception:
                    self.renew_error = 'renewal_unavailable'

    def _resource_check(self):
        from .resources import policy_violation, UNKNOWN_OBSERVATION
        try:
            observation = self._policy_observer()
            # Never leak observer internals or accept malformed OS output.
            if (not isinstance(observation, dict)
                or set(observation) != set(UNKNOWN_OBSERVATION)
                or any(not isinstance(value, str) for value in observation.values())):
                observation = dict(UNKNOWN_OBSERVATION)
        except Exception:
            observation = dict(UNKNOWN_OBSERVATION)
        self.resource_observation = observation
        return policy_violation(read_resource_policy(self.root) or {}, observation)

    def _cancel_acquisition(self):
        if self.acquisition is not None:
            self.acquisition.cancel()
        self._acquisition_active = False

    def _policy_loop(self):
        # Native observations, not persisted self-attestations. On a policy or
        # membership failure, cancel the actual acquisition worker, not only UI.
        while not self.closed.wait(1):
            with self.guard:
                if self.closed.is_set() or not self._acquisition_active:
                    continue
                try:
                    if self.acquisition.status()['state'] in {'prepared', 'cancelled', 'failed'}:
                        self._acquisition_active = False
                        continue
                    current = self.authority.status()
                    reason = ('membership_required' if not self.running or
                              current.get('membership_state') != 'current'
                              else self._resource_check())
                except Exception:
                    reason = 'resource_observation_unavailable'
                if reason:
                    self.acquisition_policy_error = reason
                    self._cancel_acquisition()

    def status(self):
        if self.closed.is_set(): raise DemoError('application_closed')
        value = self.authority.status()
        coordinator = getattr(self.authority, 'coordinator', None)
        if self.installation_role == 'owner' and coordinator is not None:
            value = {**value, 'members': [
                {'node_id':m['node_id'],'generation':m['generation'],'lifecycle_state':m['lifecycle_state']}
                for m in coordinator.members()]}
        return {'label':'macOS A13 hackathon demo','role':'not_yet_serving',
                'model_contract':model_contract(),
                'network':({'state':'private'} if self.public_relay is None else self.public_relay.status()),
                'acquisition':self.acquisition.status(),
                'resource_observation':dict(self.resource_observation),
                'acquisition_policy_error':self.acquisition_policy_error,
                'installation_role':self.installation_role,'resource_policy':read_resource_policy(self.root),
                'running':self.running,'authority':value,'renewal_error':self.renew_error,
                'recovery_error':self.recovery_error,
                'distribution':'unsigned development software; not notarized',
                'serving':{'state':'not_yet_serving','route_ready':False,
                           'next_action':'Native route preparation and exact model execution approval required.'}}

    def action(self, path, body):
        with self.guard:
            if self.closed.is_set(): raise DemoError('application_closed')
            if self.installation_role == 'target' and path in {'/api/create','/api/revoke','/api/revoke-self','/api/pair/approve','/api/pair/preview'}:
                raise DemoError('owner_role_required')
            if path == '/api/local-contributor/start':
                if body:raise DemoError('invalid_request')
                return self._start_local_contributor()
            if path == '/api/local-contributor/stop':
                if body:raise DemoError('invalid_request')
                self._stop_local_contributor()
                return {'state':'stopped','identity_retained':True,'route_ready':False}
            actor=self.authority if self.installation_role=='target' else self.local_contributor
            if path in {'/api/serving/compile','/api/serving/start','/api/serving/activate','/api/serving/status','/api/serving/stop'}:
                if self.installation_role!='owner':raise DemoError('owner_action_required')
                if path=='/api/serving/compile':
                    if set(body)!={'model_preparation_acknowledged'}:raise DemoError('model_preparation_consent_required')
                    from .chain_compile import start_compile
                    return start_compile(self,acknowledged=body['model_preparation_acknowledged'])
                if path=='/api/serving/activate':
                    if set(body)!={'execution_proposal','activation_acknowledged'}:raise DemoError('model_execution_consent_required')
                    from .chain_compile import activate_serving
                    return activate_serving(self,proposal=body['execution_proposal'],acknowledged=body['activation_acknowledged'])
                if path=='/api/serving/start':
                    if set(body)!={'execution_proposal','model_execution_acknowledged'}:raise DemoError('model_execution_consent_required')
                    from .chain_compile import start_serving
                    return start_serving(self,proposal=body['execution_proposal'],acknowledged=body['model_execution_acknowledged'])
                if body:raise DemoError('invalid_request')
                workflow=getattr(self,'_chain_workflow',None)
                if path=='/api/serving/stop' and workflow is not None:workflow.stop()
                return {'compilation':getattr(self,'_chain_compile_state',None),
                    'serving':None if workflow is None else workflow.status(),'route_ready':False}
            if path.startswith('/api/serving/observe/'):
                if self.installation_role!='owner':raise DemoError('owner_action_required')
                if path=='/api/serving/observe/prepare':
                    if set(body)!={'run_id','deployment_id','descriptors','bootstrap_acknowledged'}:
                        raise DemoError('bootstrap_owner_consent_required')
                    if getattr(self,'_observation_context',None) is not None:
                        raise DemoError('bootstrap_context_already_exists')
                    from .chain_observation import prepare_observation
                    self._observation_context=prepare_observation(self,run_id=body['run_id'],
                        deployment_id=body['deployment_id'],descriptors=body['descriptors'],
                        acknowledged=body['bootstrap_acknowledged'])
                    self._chain_target_keys=[json.loads(json.dumps(c['verification_key'])) for c in body['descriptors']]
                    return {'state':'awaiting_bootstrap_workers','route_ready':False,
                        'node_ids':list(self._observation_context.facts)}
                if path=='/api/serving/observe/collect':
                    if body!={'observe_acknowledged':True} or body['observe_acknowledged'] is not True:
                        raise DemoError('bootstrap_observation_consent_required')
                    context=getattr(self,'_observation_context',None)
                    if context is None:raise DemoError('bootstrap_context_missing')
                    return context.collect()
                raise DemoError('unknown_action')
            if path == '/api/installation/descriptor':
                if set(body)!={'run_id','deployment_id','disclosure_acknowledged'}:
                    raise DemoError('installation_disclosure_consent_required')
                from .installation_descriptor import installation_descriptor
                return installation_descriptor(self,run_id=body['run_id'],deployment_id=body['deployment_id'],
                    acknowledged=body['disclosure_acknowledged'])
            if path == '/api/network/reconnect':
                if (self.installation_role != 'target' or set(body) != {'card','relay_acknowledged'}
                        or body['relay_acknowledged'] is not True):
                    raise DemoError('network_consent_required')
                from .endpoint_recovery import reconnect_peer
                self.authority.stop_control()
                result=reconnect_peer(self.authority.peer,card=body['card'],acknowledged=True)
                self.running=True;self.recovery_error=None
                return result
            if path == '/api/network/recovery-card':
                if (self.installation_role != 'owner' or set(body) not in ({'relay_acknowledged'}, {'relay_acknowledged','previous_origin'})
                    or body['relay_acknowledged'] is not True):
                    raise DemoError('network_consent_required')
                from .endpoint_recovery import refresh_endpoint_card
                return {'recovery_card':refresh_endpoint_card(self.authority,acknowledged=True,
                    previous_origin=body.get('previous_origin'))}
            if path == '/api/network/restart':
                if (self.installation_role!='owner' or not self.authority.status().get('swarm_created')
                    or set(body)!={'relay_acknowledged','max_seconds'}
                    or body['relay_acknowledged'] is not True
                    or type(body['max_seconds']) is not int or not 30<=body['max_seconds']<=1800):
                    raise DemoError('network_consent_required')
                if getattr(self,'local_contributor',None) is not None:
                    raise DemoError('network_stop_local_contributor_required')
                if self.public_relay is not None:
                    self.public_relay.close();self.public_relay=None
                from .public_relay import TemporaryHttpsRelay
                from .endpoint_recovery import relocate_authority
                import socket
                relay=TemporaryHttpsRelay(self.root).start(acknowledged=True,max_seconds=body['max_seconds'])
                try:
                    with socket.socket() as listener:
                        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
                    card=relocate_authority(self.authority,public_origin=relay.public_origin,port=port,acknowledged=True)
                    relay.proxy.bind_seed(self.authority.seed_server)
                except BaseException:
                    relay.close();raise
                self.public_relay=relay;self.running=True
                return {'network':relay.status(),'recovery_card':card}
            if path == '/api/network/start':
                if (self.installation_role!='owner' or self.public_relay is not None
                    or self.authority.status().get('swarm_created')):
                    raise DemoError('network_requires_fresh_owner')
                if (set(body) not in ({'relay_acknowledged','max_seconds'},{'relay_acknowledged','max_seconds','provider'})
                    or body.get('provider','cloudflare')!='cloudflare'
                    or body['relay_acknowledged'] is not True
                    or type(body['max_seconds']) is not int or not 30<=body['max_seconds']<=1800):
                    raise DemoError('network_consent_required')
                from .public_relay import TemporaryHttpsRelay
                import socket
                relay=TemporaryHttpsRelay(self.root/'public-relay',provider=body.get('provider','cloudflare'))
                try:
                    relay.start(acknowledged=True,max_seconds=body['max_seconds'])
                    with socket.socket() as listener:
                        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
                    self.authority.configure_public_endpoint(public_origin=relay.public_origin,
                        port=port,acknowledged=True)
                    self.public_relay=relay
                    return relay.status()
                except BaseException:
                    relay.close();raise
            if path in {'/api/work/preview','/api/work/accept'}:
                if actor is None or not self.running:raise DemoError('target_role_required')
                policy=read_resource_policy(self.root)
                if policy is None or actor.status().get('membership_state')!='current':
                    raise DemoError('membership_required')
                reason=self._resource_check()
                if reason:raise DemoError(reason)
                previewing=path.endswith('/preview')
                expected={'limits','stage_acknowledged'} if previewing else {'limits','preview','work_acknowledged'}
                if set(body)!=expected:raise DemoError('work_consent_invalid')
                from .worker import WorkerLimits
                from .work_acceptance import preview_work,accept_work
                limits=WorkerLimits(**body['limits'])
                if limits.max_disk_bytes>policy['storage_bytes']:raise DemoError('work_limits_exceed_policy')
                def admitted(intent):
                    return self.running and not self.closed.is_set() and self._resource_check() is None
                options={'limits':limits,'resource_policy':policy,'authorize_stage':admitted,
                         '_test_observer':self._policy_observer}
                if previewing:
                    offer=actor.pending_stage()
                    if offer is None:raise DemoError('work_offer_missing')
                    return preview_work(actor,offer,stage_acknowledged=body['stage_acknowledged'],**options)
                return accept_work(actor,body['preview'],work_acknowledged=body['work_acknowledged'],**options)
            if path in {'/api/staging/cleanup/check','/api/staging/cleanup/accept'}:
                if actor is None or not self.running:raise DemoError('target_role_required')
                if path.endswith('/check'):
                    if body:raise DemoError('invalid_request')
                    return {'offer':actor.pending_cleanup()}
                if set(body)!={'offer_id','archive_digest','cleanup_acknowledged'} or body['cleanup_acknowledged'] is not True:
                    raise DemoError('stage_consent_required')
                offer=actor.pending_cleanup()
                if offer is None or offer['offer_id']!=body['offer_id'] or offer['archive_digest']!=body['archive_digest']:
                    raise DemoError('stage_offer_changed')
                ack=actor.cleanup_offer(offer,acknowledged=True)
                return {'state':'cleaned','removed':ack['removed'],'identity_retained':True,'route_ready':False}
            if path == '/api/control/stop':
                if actor is None or body:raise DemoError('invalid_request')
                return actor.stop_control()
            if path in {'/api/staging/check','/api/staging/accept','/api/control/start'}:
                if actor is None or not self.running:
                    raise DemoError('target_role_required')
                policy=read_resource_policy(self.root)
                if policy is None:raise DemoError('consent_required')
                if actor.status().get('membership_state')!='current':
                    raise DemoError('membership_required')
                if path=='/api/staging/check':
                    if body:raise DemoError('invalid_request')
                    return {'offer':actor.pending_stage()}
                consent_key='control_acknowledged' if path=='/api/control/start' else 'stage_acknowledged'
                if (set(body)!={'offer_id','archive_digest',consent_key}
                    or body[consent_key] is not True):
                    raise DemoError('stage_consent_required')
                reason=self._resource_check()
                if reason:raise DemoError(reason)
                offer=actor.pending_stage()
                if (offer is None or body['offer_id']!=offer['offer_id']
                    or body['archive_digest']!=offer['archive_digest']):
                    raise DemoError('stage_offer_changed')
                from .managed_offers import MAX_ARCHIVE
                # No URL/path/executable/limit is taken from the browser.
                limit=min(MAX_ARCHIVE,policy['storage_bytes'])
                def authorize(intent):
                    return (self.running and not self.closed.is_set()
                        and self._resource_check() is None
                        and intent.get('offer_id')==body['offer_id']
                        and intent.get('archive_digest')==body['archive_digest'])
                if path=='/api/control/start':
                    return actor.start_control(offer,acknowledged=True,resource_policy=policy,
                        max_disk_bytes=policy['storage_bytes'],_test_observer=self._policy_observer)
                ack=actor.stage_offer(offer,max_bytes=limit,
                    max_disk_bytes=policy['storage_bytes'],authorize=authorize)
                return {'state':'staged','offer_id':offer['offer_id'],
                    'archive_digest':ack['archive_digest'],'route_ready':False}
            if path == '/api/model/download':
                if set(body) != {'consent'} or not isinstance(body['consent'], dict):
                    raise DemoError('download_consent_required')
                grant = body['consent']
                policy = read_resource_policy(self.root)
                if (policy is None or not self.running
                    or self.authority.status().get('membership_state') != 'current'):
                    raise DemoError('membership_required')
                if (set(grant) != {'download_acknowledged', 'max_download_bytes', 'storage_limit_bytes'}
                    or grant['download_acknowledged'] is not True
                    or type(grant['max_download_bytes']) is not int
                    or type(grant['storage_limit_bytes']) is not int
                    or not 0 < grant['max_download_bytes'] <= policy['storage_bytes']
                    or not 0 < grant['storage_limit_bytes'] <= policy['storage_bytes']):
                    raise DemoError('download_consent_invalid')
                reason = self._resource_check()
                if reason:
                    self.acquisition_policy_error = reason
                    raise DemoError(reason)
                if self._acquisition_active:
                    raise DemoError('acquisition_already_active')
                result = self.acquisition.start(grant)
                self.acquisition_policy_error = None
                self._acquisition_active = True
                return result
            if path == '/api/model/cancel':
                if body: raise DemoError('invalid_request')
                self._cancel_acquisition()
                return self.acquisition.status()
            if path == '/api/join/request':
                if set(body) != {'consent'} or not consent_valid(body['consent']):
                    raise DemoError('consent_required')
                if self.installation_role != 'target' and self.authority.status().get('swarm_created'):
                    raise DemoError('owner_cannot_become_target')
                consent = body['consent']
                saved = read_resource_policy(self.root)
                if saved is not None and saved != consent:
                    raise DemoError('consent_retry_changed')
                if saved is None:
                    private_write(self.root / 'resource-policy.json',json.dumps(consent,sort_keys=True).encode())
                if self.installation_role != 'target':
                    self.authority.stop()
                    self.running = False
                    from .authority import _atomic_private_json
                    _atomic_private_json(self.root / 'installation-role.json',{
                        'protocol':'mycelium.a13_installation_role.v1','role':'target'})
                    from .target_app import TargetApplication
                    self.installation_role = 'target'
                    self.authority = TargetApplication(self.root / 'target',_test_peer_factory=self._test_peer_factory)
                if not self.running:
                    self.authority.start()
                    self.running = True
                if (self.acquisition is None or
                        self.acquisition.state_root != self.authority.peer.state_root / 'serving'):
                    self._open_acquisition()
                return self.authority.pair_request()
            if path == '/api/create':
                if set(body) != {'consent'} or not consent_valid(body['consent']):
                    raise DemoError('consent_required')
                consent = body['consent']
                saved=read_resource_policy(self.root)
                if saved is not None and saved!=consent: raise DemoError('consent_retry_changed')
                if saved is None:
                    private_write(self.root / 'resource-policy.json',json.dumps(consent,sort_keys=True).encode())
                current=self.authority.status()
                if current.get('swarm_created'):
                    if current.get('membership_state')=='current': return current
                    if current.get('membership_state')!='unenrolled': raise DemoError('membership_cannot_reenroll')
                else:
                    self.authority.create_swarm({
                        'cooperative_peer_visibility_acknowledged':True,
                        'resource_use_acknowledged':True,'contribution_enabled':False})
                if self.public_relay is not None:
                    self.public_relay.proxy.bind_seed(self.authority.seed_server)
                request = self.authority.pair_request()
                handoff = self.authority.approve_pairing(request, ttl_seconds=120, quota={
                    'storage_bytes':consent['storage_bytes'],
                    'network_bytes_per_day':consent['storage_bytes'],
                    'background_allowed':False})
                result = self.authority.accept_pairing(handoff)
                self.running = True
                return result
            if path == '/api/stop':
                if body: raise DemoError('invalid_request')
                self._cancel_acquisition()
                self._stop_local_contributor()
                self.authority.stop(); self.running = False
                return {'stopped':True,'identity_retained':True,'evidence_retained':True}
            if path == '/api/start':
                if body: raise DemoError('invalid_request')
                if not self.running:
                    self.authority.start(); self.running = True
                    self.recovery_error=None
                    if self.public_relay is not None:
                        self.public_relay.proxy.bind_seed(self.authority.seed_server)
                    if self.installation_role=='owner' and (self.root/'local-contributor').exists():
                        self._start_local_contributor()
                return self.status()
            if path == '/api/revoke':
                if set(body) != {'node_id','generation'}: raise DemoError('invalid_request')
                result = self.authority.revoke(body['node_id'],body['generation'])
                if getattr(self.authority.member, 'node_id', None) == body['node_id']:
                    self._cancel_acquisition()
                return result
            if path == '/api/revoke-self':
                if set(body) != {'confirm'} or body['confirm'] is not True:
                    raise DemoError('revoke_confirmation_required')
                member=self.authority.member
                if member is None or member.generation is None:
                    raise DemoError('membership_unavailable')
                result = self.authority.revoke(member.node_id,member.generation)
                self._cancel_acquisition()
                return result
            if path == '/api/pair/request':
                if body: raise DemoError('invalid_request')
                return self.authority.pair_request()
            if path == '/api/capability/report':
                if body: raise DemoError('invalid_request')
                return self.authority.report_capability()
            if path == '/api/pair/approve':
                if set(body) != {'request','ttl_seconds','quota'}: raise DemoError('invalid_request')
                return self.authority.approve_pairing(body['request'],ttl_seconds=body['ttl_seconds'],quota=body['quota'])
            if path == '/api/pair/preview':
                if set(body) != {'request'}: raise DemoError('invalid_request')
                from .pairing import verify_pairing_request
                value = verify_pairing_request(body['request'],now=time.time())
                return {key:value[key] for key in ('node_id','identity_key_digest','peer_class','expires_at')}
            if path == '/api/pair/accept':
                if set(body) != {'handoff'}: raise DemoError('invalid_request')
                return self.authority.accept_pairing(body['handoff'])
            if path == '/api/quit':
                if body: raise DemoError('invalid_request')
                threading.Thread(target=self.close,daemon=True).start()
                return {'stopping':True,'identity_retained':True,'evidence_retained':True}
            raise DemoError('unknown_action')

    def close(self):
        with self.guard:
            if self.closed.is_set(): return
            self.closed.set()
            self.session=None
            workflow=getattr(self,'_chain_workflow',None)
            if workflow is not None:
                try:workflow.stop()
                except Exception:self.cleanup_error='serving_stop_failed'
            serving_thread=getattr(self,'_chain_serving_thread',None)
            if serving_thread is not None and serving_thread is not threading.current_thread():
                serving_thread.join(timeout=15)
                if serving_thread.is_alive():self.cleanup_error='serving_stop_incomplete'
            try:self._stop_local_contributor()
            except Exception:self.cleanup_error='local_contributor_stop_failed'
            if self.acquisition is not None:
                try: self.acquisition.close()
                except Exception: self.cleanup_error='acquisition_stop_failed'
            if self.public_relay is not None:
                try: self.public_relay.close()
                except Exception: self.cleanup_error='relay_stop_failed'
            if self.authority is not None:
                try: self.authority.stop()
                except Exception: self.cleanup_error='authority_stop_failed'
            self.running = False
            if self.server is not None:
                self.server.shutdown(); self.server.server_close(); self.server = None
            self.launch_file.unlink(missing_ok=True)
            (self.root / 'native-control.json').unlink(missing_ok=True)
            if self.lock_fd is not None:
                fcntl.flock(self.lock_fd,fcntl.LOCK_UN); os.close(self.lock_fd); self.lock_fd=None
            lock = getattr(self, '_win32_instance_lock', None)
            if lock is not None:
                lock.release(); self._win32_instance_lock = None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def setup(self):
        super().setup(); self.connection.settimeout(3)
    def log_message(self, *args): pass
    @property
    def app(self): return self.server.app
    def send_value(self, status, value, *, content_type='application/json', extra=None):
        raw = value if isinstance(value, bytes) else json.dumps(value,separators=(',',':'),allow_nan=False).encode()
        self.send_response(status)
        headers={'Content-Type':content_type,'Content-Length':str(len(raw)), 'Connection':'close',
                 'Cache-Control':'no-store','Referrer-Policy':'no-referrer', 'X-Content-Type-Options':'nosniff',
                 'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"}
        headers.update(extra or {})
        for k,v in headers.items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(raw); self.close_connection=True
    def fail(self,status,code): self.send_value(status,{'error':code})
    def base_guard(self):
        if self.headers.get_all('Host') != [f'127.0.0.1:{self.app.port}']:
            self.fail(403,'host_rejected'); return False
        if '?' in self.path or '#' in self.path or '%' in self.path or '\\' in self.path:
            self.fail(400,'path_rejected'); return False
        return True
    def authorized(self):
        try:
            cookie=SimpleCookie(); cookie.load(self.headers.get('Cookie',''))
            candidate=cookie.get('a13session')
            valid = (candidate is not None and self.app.session is not None
                     and time.monotonic()<self.app.session_expires
                     and secrets.compare_digest(candidate.value,self.app.session))
        except Exception: valid=False
        if not valid: self.fail(401,'open_from_native_launcher')
        return valid
    def do_GET(self):
        if not self.base_guard(): return
        if self.path in {'/','/app.js','/style.css'}:
            name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[self.path]
            path=Path(__file__).parent/'web'/name
            if not path.is_file(): self.fail(503,'assets_unavailable'); return
            kind={'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}[self.path]
            self.send_value(200,path.read_bytes(),content_type=kind); return
        if self.path == '/api/status':
            if not self.authorized(): return
            with self.app.guard:
                try: self.send_value(200,self.app.status())
                except Exception: self.fail(503,'status_unavailable')
            return
        self.fail(404,'not_found')
    def do_POST(self):
        if not self.base_guard(): return
        if self.path == '/native-session':
            supplied=self.headers.get('X-Native-Authorization','')
            if not secrets.compare_digest(supplied,self.app.native_credential):
                self.fail(403,'native_authorization_required'); return
            if self.headers.get('Origin') is not None:
                self.fail(403,'native_only'); return
        elif self.path != '/session':
            if not self.authorized(): return
            if self.headers.get('Origin') != self.app.origin or self.headers.get('X-Mycelium-Action') != '1':
                self.fail(403,'origin_rejected'); return
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[])) != 1:
            self.fail(400,'framing_rejected'); return
        try:
            length=int(self.headers['Content-Length'])
            if length<0: raise ValueError
        except (ValueError,TypeError): self.fail(400,'framing_rejected'); return
        if length>MAX_BODY: self.fail(413,'body_too_large'); return
        try: raw=self.rfile.read(length)
        except OSError: self.fail(408,'read_timeout'); return
        if len(raw)!=length: self.fail(400,'incomplete_body'); return
        if self.path == '/native-session':
            if raw != b'{}' or self.headers.get('Content-Type') != 'application/json':
                self.fail(400,'invalid_request'); return
            with self.app.guard:
                if self.app.closed.is_set(): self.fail(409,'application_closed'); return
                self.app.make_launch_file()
                self.send_value(200,{'launch_file_ready':True})
            return
        if self.path == '/session':
            if self.headers.get('Content-Type')!='application/x-www-form-urlencoded':
                self.fail(415,'content_type_rejected'); return
            if self.headers.get('Origin') not in {'null',self.app.origin}:
                self.fail(403,'origin_rejected'); return
            try: fields=urllib.parse.parse_qs(raw.decode('ascii'),strict_parsing=True)
            except (ValueError,UnicodeError): self.fail(400,'invalid_request'); return
            with self.app.guard:
                if self.app.closed.is_set(): self.fail(409,'application_closed'); return
                if (set(fields)!={'launch'} or len(fields['launch'])!=1 or self.app.launch_code is None
                    or time.monotonic()>=self.app.launch_expires
                    or not secrets.compare_digest(fields['launch'][0],self.app.launch_code)):
                    self.fail(403,'launch_expired'); return
                self.app.launch_code=None
                self.app.launch_file.unlink(missing_ok=True)
                self.app.session=secrets.token_urlsafe(32)
                self.app.session_expires=time.monotonic()+3600
                self.send_value(303,b'',extra={'Location':'/','Set-Cookie':f'a13session={self.app.session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600'})
            return
        if self.headers.get('Content-Type') != 'application/json':
            self.fail(415,'content_type_rejected'); return
        try:
            def pairs(items):
                value={}
                for k,v in items:
                    if k in value: raise ValueError
                    value[k]=v
                return value
            body=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(body,dict): raise ValueError
        except (ValueError,UnicodeError): self.fail(400,'invalid_request'); return
        try: result=self.app.action(self.path,body)
        except DemoError as e: self.fail(400,str(e)); return
        except Exception as error:
            code=public_failure_code(error)
            # Record WHY before answering. The public body stays a closed code;
            # this private one-line identity is what makes the next failure
            # self-describing instead of another generic 409 investigation.
            try:
                private_write(self.app.root/'last-action-error.private.json',
                    json.dumps(bounded_action_failure(self.path,error,code),sort_keys=True).encode())
            except BaseException:
                pass
            self.fail(409,code); return
        self.send_value(200,result)
