# SPDX-License-Identifier: AGPL-3.0-only
"""Private, loopback-only macOS demo application. Never a public admin server."""
from __future__ import annotations

try:  # instance lock; win32 goes through mycelium_host_platform
    import fcntl
except ImportError:  # pragma: no cover - win32
    fcntl = None
from mycelium_http_identity import with_user_agent
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
from . import a13_test_seams as _test_seams
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


def model_contract(root=None):
    """The run model before/without state: env override, else the demo default.

    With a state root the persisted picker choice applies (run_model.py).
    """
    from .run_model import app_run_model, demo_default_choice, choice
    from .serving import RUN_MODEL_ENVIRONMENT, selected_run_model
    if root is not None:
        return asdict(app_run_model(root)[2])
    if RUN_MODEL_ENVIRONMENT in os.environ:
        return asdict(selected_run_model())
    return asdict(choice(demo_default_choice()).model)


def _test_seam_offset(app):
    """The forced member clock offset actually in force (target role only); None = no seam (the normal case)."""
    return getattr(app.authority, 'test_clock_offset_seconds', None) if app.installation_role == 'target' else None


class DemoError(RuntimeError):
    pass


class DemoActionError(DemoError):
    """A refusal whose response also says what to do (``message`` and ``fix``)."""

    def __init__(self, code, *, message, fix):
        super().__init__(code)
        self.message = message
        self.fix = fix


def action_error_body(error):
    """The public JSON body of a refused action: a closed code, plus advice if any."""
    body = {'error': str(error)}
    if isinstance(error, DemoActionError):
        body.update(message=error.message, fix=dict(error.fix))
    return body


def _admitted_macos(os_version):
    if os_version == '15.6.1':
        return True
    parts = os_version.split('.') if isinstance(os_version, str) else []
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() and len(p) <= 3 for p in parts):
        return False
    return int(parts[0]) >= 26


#: The open-ended floor the refusal message names: macOS 26.0 and later are
#: admitted, plus 15.6.1 as an exact target.  Info.plist LSMinimumSystemVersion
#: is the lowest admitted release (build_a13_macos.py), so Finder opens the app
#: on 15.6.1 even though the bundled Homebrew runtime is stamped minos 26.0.
MACOS_FLOOR = '26.0'


def unsupported_mac_message(*, system, machine, os_version):
    """Plain words for a person whose Mac cannot run this build."""
    shown = os_version if isinstance(os_version, str) and len(os_version) <= 16 else 'unknown'
    if system == 'Darwin' and machine != 'arm64':
        return ('This Mac has an Intel processor (macOS %s). Mycelium A13 Demo needs an Apple Silicon '
                'Mac (M1 or later) with macOS %s or later, because the model runs on the Apple GPU '
                'with MLX. You can still use the shared chat page from this Mac\'s browser.'
                % (shown, MACOS_FLOOR))
    if system == 'Darwin':
        return ('This Mac runs macOS %s. Mycelium A13 Demo needs macOS %s or later. Update macOS in '
                'System Settings > General > Software Update, then open the app again. Until then you '
                'can still use the shared chat page from this Mac\'s browser.' % (shown, MACOS_FLOOR))
    return ('This build of Mycelium A13 Demo runs on Apple Silicon Macs with macOS %s or later.'
            % MACOS_FLOOR)


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
        error.user_message=unsupported_mac_message(system=system,machine=machine,os_version=os_version)
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
    # The storage ceiling is derived per model (model_resources.py): the
    # largest single enforced storage item any registered contract can need on
    # one host.  Qwen3-14B sets it: a member holding the whole model needs
    # 1.1 x (29,536,614,400 B weights + 4 MiB headers + 64 MiB control archive)
    # = 32,568,709,325 B (its owner download need, snapshot + 10%, is
    # 32,507,875,154 B).  The measured 7B seeded path stays expressible
    # (24,854,140,006 B; laptop snapshot + stage copy 23,052,975,062 B, G2 D-1).
    # It must be expressible, because the policy also bounds the acquisition's
    # storage grant, the slice fetch and the worker's staging disk.  Whether
    # it suffices for the SELECTED model is checked when a run starts.
    from .model_resources import storage_consent_ceiling
    return (isinstance(c, dict) and set(c) == {'peer_visibility','power','storage_bytes','network','background','diagnostics','thermal'}
            and c['peer_visibility'] is True and c['power'] in {'ac_only','battery_allowed'}
            and type(c['storage_bytes']) is int and 100_000_000 <= c['storage_bytes'] <= storage_consent_ceiling()
            and c['network'] in {'unmetered_only','metered_allowed'}
            and c['background'] is False and c['diagnostics'] is False
            and c['thermal'] == 'stop_on_serious')


def pairing_quota_valid(quota):
    """The owner may approve a joining Mac's quota up to the largest member share
    any registered model can give one Mac (model_resources.pairing_quota_ceiling)."""
    from .model_resources import pairing_quota_ceiling
    cap = pairing_quota_ceiling()
    return (isinstance(quota, dict) and type(quota.get('storage_bytes')) is int
            and type(quota.get('network_bytes_per_day')) is int
            and quota['storage_bytes'] <= cap and quota['network_bytes_per_day'] <= cap)


def host_memory_bytes():
    """This Mac's physical memory (no subprocess); ``None`` when unknown."""
    try:
        value = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except (ValueError, OSError, AttributeError):
        return None
    return value if type(value) is int and value > 0 else None


#: Optional body keys of an external-provider relay request (public_origin and proxy_port are required).
_EXTERNAL_OPTIONAL=frozenset({'provider','public_origin','proxy_port','cloudflare_front','auto_renew','long_lived',
                              'max_renewed_seconds'})


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
        connection.request('POST','/native-session','{}',headers=with_user_agent({
            'Content-Type':'application/json','X-Native-Authorization':control['credential']}))
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
    window = seed_time_window(error)
    if window is not None:
        record['seed_window'] = window
    return record


_SEED_WINDOW_FIELDS = ('now', 'issued_at', 'expires_at', 'seconds_before_issued',
                       'seconds_after_expires', 'max_clock_skew_seconds')


def seed_time_window(error):
    """The window numbers of a `seed_http_seed_time_invalid`, or None.

    `SeedHTTPClient` attaches them to the SeedHTTPError it raises, but the peer
    layer re-raises a code-only error `from` it, so walk the (bounded) cause
    chain. Only allowlisted keys, each a finite float or None: never text.
    """
    import math
    seen = 0
    current = error
    while current is not None and seen < 8:
        window = getattr(current, 'window', None)
        if isinstance(window, dict):
            out = {}
            for key in _SEED_WINDOW_FIELDS:
                if key in window:
                    value = window[key]
                    out[key] = (float(value) if type(value) in (int, float) and math.isfinite(value)
                                else None)
            return out
        current = current.__cause__ or current.__context__
        seen += 1
    return None


def seed_time_refusal_event(path, public_code, record):
    """One bounded daemon-log line for a seed window refusal; None otherwise."""
    window = record.get('seed_window') if isinstance(record, dict) else None
    if window is None:
        return None
    from mycelium_membership import MAX_CLOCK_SKEW_SECONDS
    return {'event': 'seed_time_refused', 'path': record.get('path', '/invalid'),
            'public_code': record.get('public_code', 'action_not_completed'),
            **({'error_code': record['error_code']} if 'error_code' in record else {}),
            'max_clock_skew_seconds': MAX_CLOCK_SKEW_SECONDS, 'seed_window': window}


def _swarm_status(app):
    """'Publish this run as a swarm' status; never lets it break /api/status."""
    try:
        from .swarm_publish import status
        return status(app)
    except Exception:
        return {'state':'unavailable'}


def _sharing_status(app):
    """Derived Paused -> Rejoining -> Sharing (additive; never lets it break /api/status)."""
    try:
        from .sharing_state import sharing_state
        return sharing_state(app, observe_only=True)
    except Exception:
        return {'state':'idle','line':'Not sharing yet'}


def _peer_liveness():
    """Per-peer liveness (fresh/stale/quarantined, last seen) of the route open in this process, else None."""
    try:
        from mycelium_live.route import open_route_peer_liveness
        return open_route_peer_liveness()
    except Exception:
        return None


def _demo_status(app, part):
    """Status of the one-click demo pieces; never lets them break /api/status."""
    try:
        from . import demo_app
        if part == 'owner': return demo_app.owner_status(app, observe_only=True)
        if part == 'target': return demo_app.target_status(app)
        from .chat import chat_status
        return chat_status(app)
    except Exception:
        return {'state':'unavailable'}


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


_CONSENT_ROUTES = frozenset({'/api/create','/api/join/request','/api/join/window'})


class DemoApp:
    def __init__(self, state_root, *, authority_factory=None, check_platform=True, _test_peer_factory=None,
                 _test_join_base_url=None):
        self.root = Path(state_root).expanduser().absolute()
        # Join flow (pairing window, standing consent, automatic reports).
        # ``_test_join_base_url`` is a deterministic local-test seam only: it
        # sends the target's window/probe/report requests to a loopback relay
        # proxy instead of the public HTTPS origin.
        self._test_join_base_url = _test_join_base_url
        from .pairing import PairingWindows
        from .capability import MemberReports
        self.pairing_windows = PairingWindows()
        self.member_reports = MemberReports(lookup=self._member_lookup)
        self.standing = None
        self.standing_error = None
        self.standing_error_http = None
        #: Click-free target steps another lane registers: fn(app, context) -> dict | None
        #: (see standing_consent.auto_poll_step).  Run only under standing consent.
        self.auto_steps = []
        # One-click demo run (demo_app.py): owner run + exchange, joined-Mac agent.
        from . import demo_app as _demo_app
        self.run_exchange = None
        self.owner_run = None
        self.demo_target = None
        self.chat = None
        self.auto_steps.append(_demo_app.auto_step)
        self.last_report = None
        self._next_periodic_report = 0.0
        self._probe_times = []
        self.check_platform = check_platform
        self.authority_factory = authority_factory
        self._test_peer_factory = _test_peer_factory
        self.installation_role = 'owner'
        self.authority = None
        self.server = None
        self.thread = None
        self.lock_fd = None
        self.guard = threading.RLock()
        self.status_publisher = None
        self._pending_windows = []
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
                self.authority.adaptive_overlay = self._adaptive_overlay
                self.authority.adaptive_drain = self._adaptive_drain
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
                    or exc.code not in {'peer_transport_unreachable', 'seed_http_remote_error',
                                        'peer_member_expired', 'peer_generation_stale', 'peer_member_unknown'}):
                    raise
                # A dead relay must not remove the local authenticated recovery
                # surface. No member, native worker or contribution is enabled.  The same holds for a lapsed
                # membership (V1.1 d): the app starts so the owner's new invite can replace it (``/api/join/window``).
                self.recovery_error = exc.code
                self.running = False
            self._open_acquisition()
            self.server = _Server(('127.0.0.1', 0), _Handler)
            self.server.app = self
            from .published_status import PublishedStatus
            self.status_publisher = PublishedStatus(self)
            self.status_publisher.start()
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            private_write(self.root / 'native-control.json',json.dumps({
                'port':self.port,'credential':self.native_credential}).encode())
            self.make_launch_file()
            self.renew_thread = threading.Thread(target=self._renew_loop, daemon=True)
            self.renew_thread.start()
            self.policy_thread = threading.Thread(target=self._policy_loop, daemon=True)
            self.policy_thread.start()
            self.standing_thread = threading.Thread(target=self._standing_loop, daemon=True)
            self.standing_thread.start()
            if self.installation_role == 'target': self._resume_leave()
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
        from .serving import CANDIDATE_MODEL as _CANDIDATE
        model = self.run_model_contract()
        # A non-default run model keeps its own cache and status, never the candidate's.
        suffix = '' if model == _CANDIDATE else '-' + model.resolved_commit[:12]
        # A target's cache stays inside the staging consumer's confined 'serving'
        # root, one directory per run model: reopening another model's durable
        # status there is (correctly) acquisition_state_corrupt.
        if self.installation_role == 'target':
            serving = self.authority.peer.state_root / 'serving'
            if suffix:
                private_dir(serving)
            root = serving / ('model-source' + suffix) if suffix else serving
        else:
            root = self.root / ('model-source' + suffix)
        self.acquisition = SelectedModelAcquisition(root, model=model)
        self._acquisition_active = False

    # -- demo run: model picker, owner one-click run, target auto loop, chat ---

    def _start_public_relay(self, body):
        """Start the temporary public relay: before the swarm exists, or later (R23: join codes any time).

        A swarm created without a relay keeps its identity: the retained authority is reopened on the new
        origin (``endpoint_recovery.attach_public_endpoint``; an earlier relay's origin goes through the
        existing ``relocate_authority``), then the same route binding as a fresh start runs.  Any failure
        closes the relay and leaves the swarm exactly as it was.
        """
        if self.installation_role!='owner':
            raise DemoError('network_requires_fresh_owner')
        if self.public_relay is not None:
            # A relay whose window ran out closed itself but is still attached: the same Start reopens it (V1.2 D4).
            # One that is still running is not started a second time.
            if self.public_relay.status().get('state')!='stopped':raise DemoError('network_requires_fresh_owner')
            self.public_relay.close();self.public_relay=None
        external=body.get('provider')=='external'
        _base={'relay_acknowledged','max_seconds'}
        shape_ok=(_base|{'provider','public_origin','proxy_port'}<=set(body)<=_base|_EXTERNAL_OPTIONAL
                  if external else
                  set(body) in (_base,_base|{'provider'}) and body.get('provider','cloudflare')=='cloudflare')
        if (not shape_ok or body['relay_acknowledged'] is not True
            or type(body['max_seconds']) is not int or not 30<=body['max_seconds']<=1800):
            raise DemoError('network_consent_required')
        existing=bool(self.authority.status().get('swarm_created'))
        if (existing and getattr(self,'local_contributor',None) is not None
                and not self._external_origin_unchanged(body)):
            raise DemoError('network_stop_local_contributor_required')
        from . import endpoint_recovery
        relay=self._new_relay(self.root/'public-relay',body)
        try:
            relay.start(acknowledged=True,max_seconds=body['max_seconds'])
            port=self._free_seed_port(avoid=getattr(relay,'_proxy_port',None))
            configured=self.authority._network_configuration() if existing else None
            if external and configured is not None and configured['public_origin']==relay.public_origin:
                # A stable owner-chosen address that did not change: the retained authority already answers on it.
                relay.proxy.bind_seed(self.authority.seed_server)
                self.running=True
            elif not existing:
                self.authority.configure_public_endpoint(public_origin=relay.public_origin,
                    port=port,acknowledged=True)
            else:
                if self.authority._network_configuration() is None:
                    endpoint_recovery.attach_public_endpoint(self.authority,public_origin=relay.public_origin,
                        port=port,acknowledged=True)
                else:
                    endpoint_recovery.relocate_authority(self.authority,public_origin=relay.public_origin,
                        port=port,acknowledged=True)
                relay.proxy.bind_seed(self.authority.seed_server)
                self.running=True
            self.public_relay=relay
            if external:self._remember_external_address(body)
            self._bind_public_routes()
            self._materialize_pending_windows()
            return relay.status()
        except BaseException:
            self.public_relay=None
            relay.close();raise

    def _external_origin_unchanged(self,spec):
        """True when an external relay comes back on the address the retained authority already answers on.

        Nothing is relocated then, so the owner's own contributor need not be stopped (V1.2 D4).  Any other
        address keeps ``network_stop_local_contributor_required``.
        """
        if spec.get('provider')!='external':return False
        try:
            from .public_relay import validate_external_origin
            configured=self.authority._network_configuration()
            return configured is not None and configured['public_origin']==validate_external_origin(spec.get('public_origin'))
        except Exception:return False

    def _new_relay(self,root,spec):
        """A not-yet-started relay for a validated request body; bad owner-chosen values are bounded codes."""
        from .public_relay import TemporaryHttpsRelay
        if spec.get('provider')!='external':return TemporaryHttpsRelay(root,provider=spec.get('provider','cloudflare'))
        try:
            relay=TemporaryHttpsRelay(root,provider='external',public_origin=spec['public_origin'],
                proxy_port=spec['proxy_port'],cloudflare_front=spec.get('cloudflare_front',False),
                auto_renew=spec.get('auto_renew',True),long_lived=spec.get('long_lived',False),
                **({'max_renewed_seconds':spec['max_renewed_seconds']} if 'max_renewed_seconds' in spec else {}))
        except ValueError as error:raise DemoError(str(error)) from None
        relay.set_serving_probe(self._relay_is_serving)
        return relay

    def _relay_is_serving(self):
        """True while an owner run is active or a swarm is published: the external relay may extend itself only then (V1.1 e, V1.2 D4)."""
        run=getattr(self,'owner_run',None)
        if run is not None and run.active() is True:return True
        # A published swarm's listing points at this relay: keep it up (bounded by the same renewal cap) while idle.
        publisher=getattr(self,'swarm_publisher',None)
        state=getattr(publisher,'state',None)
        return isinstance(state,dict) and state.get('enabled') is True

    @staticmethod
    def _free_seed_port(avoid=None):
        while True:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            if port!=avoid:return port

    def _external_address_path(self):
        return self.root/'external-origin.json'

    def _remember_external_address(self,spec):
        """The owner-chosen address is kept so a later restart (budget ran out, app relaunch) reuses it."""
        value={'public_origin':spec['public_origin'],'proxy_port':spec['proxy_port'],
               'cloudflare_front':spec.get('cloudflare_front',False) is True}
        from .authority import _atomic_private_json
        _atomic_private_json(self._external_address_path(),value)

    def _saved_external_address(self):
        from .authority import _load_private_json
        try:value=_load_private_json(self._external_address_path())
        except Exception:return {}
        return value if isinstance(value,dict) and set(value)=={'public_origin','proxy_port','cloudflare_front'} else {}

    # -- join codes issued before the relay exists --------------------------------------------
    def _open_pending_window(self, ttl_seconds):
        """A code asked for while no relay runs: kept as 'ready to share once the relay starts'.

        The code text and link embed the relay origin, which (quick tunnel) exists only once the relay
        starts, so the real window is opened then (``_materialize_pending_windows``) and the code is
        redeemable from that moment.  Same capacity and TTL bounds as a live window.
        """
        from .pairing import _MAX_WINDOWS, _WINDOW_TTL_MAX, _WINDOW_TTL_MIN
        if type(ttl_seconds) is not int or not _WINDOW_TTL_MIN<=ttl_seconds<=_WINDOW_TTL_MAX:
            raise DemoError('pairing_window_ttl_invalid')
        live=sum(1 for w in self.pairing_windows.list() if w['state'] in {'open','claimed','approved'})
        if live+len(self._pending_windows)>=_MAX_WINDOWS:
            raise DemoError('pairing_window_capacity')
        pending={'pending_id':'pending-'+secrets.token_hex(3),'ttl_seconds':ttl_seconds,
                 'state':'waiting_for_relay','note':'ready to share once the relay starts'}
        self._pending_windows.append(pending)
        return {'state':'waiting_for_relay','pending_id':pending['pending_id'],
                'message':'Ready to share once the relay starts: start the public relay and this code becomes redeemable.',
                'fix':{'action':'start_relay'}}

    def _materialize_pending_windows(self):
        pending,self._pending_windows=list(self._pending_windows),[]
        origin=None if self.public_relay is None else self.public_relay.status().get('public_origin')
        coordinator=getattr(self.authority,'coordinator',None)
        if not origin or coordinator is None: self._pending_windows=pending;return
        from .pairing import PairingError
        for item in pending:
            try:
                self.pairing_windows.open(origin=origin,seed_key_digest=coordinator.signer.verification_key_digest,
                    ttl_seconds=item['ttl_seconds'])
            except PairingError:
                continue  # capacity or an invalid TTL: the owner can open a fresh code

    def members_joined_without_relay(self):
        """Joined members (the owner excluded) of a swarm whose relay is not running; 0 otherwise."""
        if self.installation_role!='owner' or self.public_relay is not None: return 0
        coordinator=getattr(self.authority,'coordinator',None)
        if coordinator is None or not self.authority.status().get('swarm_created'): return 0
        try: return max(0,len(coordinator.members())-1)
        except Exception: return 0

    def _relay_wait_status(self):
        count=self.members_joined_without_relay()
        if count<1: return None
        return {'state':'waiting_for_relay','members_joined':count,
                'line':f"{count} member{'' if count==1 else 's'} joined, waiting for relay to serve"}

    def run_model_contract(self):
        """The model this installation's runs pin (picker choice, env override)."""
        from .run_model import app_run_model
        return app_run_model(self.root)[2]

    def _choose_run_model(self, body):
        from .run_model import app_run_model, save_choice, choice, document
        if set(body) != {'choice'}: raise DemoError('invalid_request')
        try: choice(body['choice'])
        except ValueError: raise DemoError('run_model_choice_invalid') from None
        if app_run_model(self.root)[1] == 'environment':
            raise DemoError('run_model_locked_by_environment')
        if self._acquisition_active: raise DemoError('acquisition_already_active')
        run = getattr(self, 'owner_run', None)
        if run is not None and run.active(): raise DemoError('owner_run_active')
        save_choice(self.root, body['choice'])
        self.acquisition_policy_error = None
        self._open_acquisition()
        return document(self.root)

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

    #: Member leave (member_leave.py): fence new work / stop lease renewal once leaving.
    _leave_fence = False
    _left = False
    _leave_thread = None

    def _leave_status(self):
        if self.installation_role != 'target': return None
        from .member_leave import leave_status
        return leave_status(self)

    def _resume_leave(self):
        try:
            from .member_leave import resume_leave
            return resume_leave(self)
        except Exception:
            return None  # a leave that cannot resume never blocks the app from starting

    def _stop_local_contributor(self):
        if self.local_contributor is not None:
            self.local_contributor.stop()
            self.local_contributor=None

    def _renew_loop(self):
        # Seed heartbeats never wait for self.guard: the standing tick holds it
        # across managed-control round trips (10 s timeout each, peer.py), so on
        # a slow link a joined Mac's heartbeat came late and its lease aged.
        # The authority and the contributor serialize their own state (their
        # renew() runs under their own lock); read the current ones and renew.
        while not self.closed.wait(20):
            if self.closed.is_set() or not self.running or getattr(self, '_left', False): continue
            authority, contributor = self.authority, self.local_contributor
            if authority is None: continue
            try:
                authority.renew()
                if contributor is not None:contributor.renew()
                self.renew_error = None
            except Exception:
                if self.running and not self.closed.is_set():
                    self.renew_error = 'renewal_unavailable'

    def _resource_check(self):
        if getattr(self, '_leave_fence', False): return 'owner_stop'  # member leave: no new work (existing A16 reason)
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
        violation = policy_violation(read_resource_policy(self.root) or {}, observation)
        if violation:
            return violation  # A13 power/thermal/network gates are never softened
        # A16 optional adaptive host-resource admission (off unless opted in).
        from .adaptive_host import adaptive_resource_check
        return adaptive_resource_check(self)

    def _adaptive_overlay(self, *, wall_clock=time.time, capacity_root=None):
        """A16 node overlay for the managed runtime's publisher (None while A16 is off).

        ``capacity_root`` (the node's private directory) lets the controller restore and keep
        current the persisted withdraw marker stored beside the overlay.
        """
        from .adaptive_host import adaptive_overlay_provider
        return adaptive_overlay_provider(self)(wall_clock=wall_clock, capacity_root=capacity_root)

    def _adaptive_drain(self, runtime):
        """A16 drain-to-withdraw tick from the managed runtime's control loop (no-op while off)."""
        adaptive = getattr(self, '_adaptive_runtime', None)
        if adaptive is None or not adaptive.controller.enabled:
            return
        from .adaptive_drain import drain_step, product_hooks
        from .adaptive_reentry import feed_for
        cached = getattr(self, '_adaptive_hooks', None)
        if cached is None or cached[0] is not runtime:
            cached = self._adaptive_hooks = (runtime, product_hooks(runtime))
        drain_step(adaptive.controller, runtime, hooks=cached[1])
        feed_for(self).tick(adaptive.controller, self.authority)

    def _adaptive_tick(self):
        """A16 tick from the policy loop while no managed runtime is attached (restart fence, re-entry)."""
        from .adaptive_host import adaptive_detached_tick
        adaptive_detached_tick(self)

    def reverify_model_snapshot(self):
        """Owner Start after a relaunch: re-hash a snapshot already on disk instead of asking for a new download.

        Uses only the standing storage consent, fetches nothing (see ``reverify_closed_snapshot``), keeps every
        power/thermal/network gate, and returns whether a verification is now running.
        """
        with self.guard:
            policy = read_resource_policy(self.root)
            if (self.closed.is_set() or policy is None or not self.running or self.acquisition is None
                    or self._acquisition_active or self.authority.status().get('membership_state') != 'current'):
                return False
            if self.acquisition.status().get('state') not in {'closed', 'idle', 'failed', 'cancelled'}:
                return False
            reason = self._resource_check()
            if reason:
                self.acquisition_policy_error = reason
                return False
            try:
                self.acquisition.reverify_closed_snapshot(storage_limit_bytes=policy['storage_bytes'])
            except Exception:
                return False
            self.acquisition_policy_error = None
            self._acquisition_active = True
            return True

    def _cancel_acquisition(self):
        if self.acquisition is not None:
            self.acquisition.cancel()
        self._acquisition_active = False

    def _policy_loop(self):
        # Native observations, not persisted self-attestations. On a policy or
        # membership failure, cancel the actual acquisition worker, not only UI.
        while not self.closed.wait(1):
            try:
                self._adaptive_tick()  # outside the guard: re-entry makes authority round trips
            except Exception:
                pass  # an A16 fault never stops the acquisition policy checks
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

    # -- join flow: pairing window, standing consent, automatic reports --------

    def _member_lookup(self, node_id):
        coordinator = getattr(self.authority, 'coordinator', None) if self.installation_role == 'owner' else None
        if coordinator is None: return None
        try: member = coordinator.member(node_id)
        except Exception: return None
        return {'verification_key_digest':member['verification_key_digest'],
                'generation':member['generation'],'lifecycle_state':member['lifecycle_state']}

    def member_capability_reports(self):
        """Interface for the compile lane: latest verified self-report per member."""
        return self.member_reports.latest()

    def _public_route(self, method, path, body):
        """Owner routes reached through the relay proxy; never takes the app guard."""
        import json as _json
        from .pairing import PAIRING_WINDOW_PREFIX
        from .capability import LINK_PROBE_PATH, MEMBER_REPORT_PATH, link_probe_response
        from .join_page import is_join_page, render as _join_page
        if is_join_page(path):
            if method != 'GET': return 405, b'{"error":"method_not_allowed"}'
            return _join_page(self.pairing_windows, path)  # QR join page (text/html)
        if path.startswith(PAIRING_WINDOW_PREFIX):
            return self.pairing_windows.handle(method, path, body)
        if path == MEMBER_REPORT_PATH:
            return self.member_reports.handle(method, path, body)
        if path.startswith(LINK_PROBE_PATH + '/'):
            kib = path[len(LINK_PROBE_PATH) + 1:]
            if method != 'POST' or not kib.isdigit() or len(kib) > 3:
                return 404, b'{"error":"route_unknown"}'
            now = time.monotonic()
            self._probe_times = [t for t in self._probe_times if now - t < 10][-64:]
            if len(self._probe_times) >= 64: return 429, b'{"error":"link_probe_rate_limited"}'
            self._probe_times.append(now)
            try: return 200, link_probe_response(int(kib), body)
            except Exception: return 400, b'{"error":"link_probe_invalid"}'
        return 404, _json.dumps({'error':'route_unknown'}).encode()

    def _bind_public_routes(self):
        if self.public_relay is not None and getattr(self.public_relay, 'proxy', None) is not None:
            self.public_relay.proxy.bind_local(self._public_route)
            bind_chat = getattr(self.public_relay.proxy, 'bind_chat', None)
            if bind_chat is not None:
                from .chat import handle as _chat_handle
                bind_chat(lambda method, path, headers, body: _chat_handle(
                    self, method, path, headers, body, via_relay=True))
            from .swarm_entry import bind as _bind_swarm_entry
            _bind_swarm_entry(self, self.public_relay.proxy)

    def _join_client(self, origin):
        from .pairing import JoinWindowClient
        return JoinWindowClient(origin, _test_base_url=self._test_join_base_url)

    def _join_state_path(self):
        return self.root / 'join-window.json'

    def _load_join_state(self):
        from .authority import _load_private_json
        value = _load_private_json(self._join_state_path())
        if value is None: return None
        if set(value) != {'protocol','origin','window_id','code','seed_key_digest','request_digest','fingerprint'} \
                or value['protocol'] != 'mycelium.a13_join_window_state.v1':
            raise DemoError('join_state_invalid')
        return value

    def _become_target(self, consent):
        """Shared by both join routes: persist consent, select the target role once."""
        if self.installation_role != 'target' and self.authority.status().get('swarm_created'):
            raise DemoError('owner_cannot_become_target')
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
            self.authority.adaptive_overlay = self._adaptive_overlay
            self.authority.adaptive_drain = self._adaptive_drain
        if not self.running:
            self.authority.start()
            self.running = True
        if (self.acquisition is None or
                self.acquisition.state_root != self.authority.peer.state_root / 'serving'):
            self._open_acquisition()

    def _device_memory_bytes(self):
        """RAM bounding this Mac's per-run memory consent (test seam)."""
        return host_memory_bytes()

    def _measure_and_sign(self, client, purpose):
        from .capability import observe_device, measure_link
        peer = self.authority.peer
        link = measure_link(client.probe)
        device = observe_device(peer.state_root)
        return peer.signed_capability_link_report(purpose=purpose, device=device, link=link)

    def _auto_report(self, purpose, *, seed_capability=None):
        """Best-effort automatic report on join, before each run and periodically.

        Sends the canonical signed seed capability message and the extended
        device + relay-link report to the owner.  Never qualifies anything and
        never blocks the caller: failures are recorded with a closed code.
        """
        record = {'purpose':purpose,'at':time.time(),'state':'delivered','error':None,
                  'seed_capability':'not_sent'}
        try:
            origin = self.authority.peer.owner_origin()
            if origin is None: raise DemoError('membership_required')
            client = self._join_client(origin)
            client.member_report(self._measure_and_sign(client, purpose))
        except Exception as error:
            record.update(state='failed', error=public_failure_code(error)
                          if not isinstance(error, DemoError) else str(error))
        try:
            if seed_capability is None:
                self.authority.report_capability()
            record['seed_capability']='accepted'
        except Exception as error:
            record['seed_capability'] = public_failure_code(error)
        self.last_report = record
        self._next_periodic_report = time.monotonic() + 300
        return dict(record)

    # Duck-typed surface for standing_consent.auto_poll_step (stable interface b).
    def auto_step_context(self):
        binding = self.authority.peer.membership_binding() if self.installation_role == 'target' else None
        if binding is not None and self.authority.status().get('membership_state') != 'current':
            binding = None
        return {'binding':binding,'policy':read_resource_policy(self.root)}

    def auto_report(self, purpose):
        return self._auto_report(purpose)

    def auto_accept(self):
        from .standing_consent import StandingAcceptor
        policy = read_resource_policy(self.root)
        binding = self.auto_step_context()['binding']
        if policy is None or binding is None: return None
        from .demo_app import holds_offer
        if holds_offer(self): return None  # bootstrap offer / slice not verified yet
        if self.standing is None:
            self.standing = StandingAcceptor(self.root, report=lambda purpose, _offer: self._auto_report(purpose),
                                             limits_for=self._member_limits_for)
        def admitted(_intent):
            return self.running and not self.closed.is_set() and self._resource_check() is None
        return self.standing.tick(self.authority, policy=policy, binding=binding,
            resource_check=self._resource_check, authorize_stage=admitted,
            _test_observer=self._policy_observer)

    def _member_limits_for(self, offer):
        """This Mac's share-derived worker limits for the offer's run (or None)."""
        agent = getattr(self, 'demo_target', None)
        return None if agent is None else agent.member_limits(offer)

    def _standing_tick(self):
        from .standing_consent import auto_poll_step
        due = time.monotonic() >= self._next_periodic_report
        return auto_poll_step(self, report_due=due)

    def _standing_loop(self):
        from .control_failures import error_http
        while not self.closed.wait(2):
            with self.guard:
                if self.closed.is_set() or not self.running or self.installation_role != 'target':
                    continue
                try:
                    self._standing_tick(); self.standing_error = None
                    self.standing_error_http = None
                except Exception as error:
                    self.standing_error = public_failure_code(error)
                    self.standing_error_http = error_http(error, self.standing_error)

    def _join_flow_status(self):
        from .standing_consent import DISCLOSURE_TEXT, DISCLOSURE_DIGEST, load_standing_consent
        value = {'standing_disclosure':{'text':DISCLOSURE_TEXT,'digest':DISCLOSURE_DIGEST}}
        if self.installation_role == 'owner':
            value['pairing_windows'] = self.pairing_windows.list()
            value['pending_windows'] = [dict(p) for p in self._pending_windows]
            value['member_reports'] = self.member_reports.latest()
            return value
        try: grant = load_standing_consent(self.root)
        except Exception: grant = None
        try: join = self._load_join_state()
        except Exception: join = None
        value.update(
            standing_consent=None if grant is None else {
                'state':grant['state'],'caps':dict(grant['caps']),'granted_at':grant['granted_at'],
                'withdrawn_at':grant['withdrawn_at'],
                'last':None if self.standing is None else self.standing.status()['last']},
            standing_error=self.standing_error,
            standing_error_http=(dict(self.standing_error_http)
                if getattr(self, 'standing_error_http', None) is not None else None),
            join_window=None if join is None else {'state':'waiting_for_owner',
                'fingerprint':join['fingerprint'],'pinned':join['seed_key_digest'] is not None},
            last_report=None if self.last_report is None else dict(self.last_report))
        return value

    def read_status(self):
        """Published display state only; never use for action admission."""
        if self.status_publisher is None: raise DemoError('status_unavailable')
        return self.status_publisher.read()

    def status(self):
        # Maintenance aggregation; HTTP reads use read_status(), never this
        # lock/I/O-bearing path or its existing refresh side effects.
        if self.closed.is_set(): raise DemoError('application_closed')
        from .demo_app import ensure_exchange
        try: ensure_exchange(self)
        except Exception: pass
        value = self.authority.status()
        coordinator = getattr(self.authority, 'coordinator', None)
        if self.installation_role == 'owner' and coordinator is not None:
            offsets = getattr(getattr(self, 'run_exchange', None), 'clock_offsets', None)
            value = {**value, 'members': [
                {'node_id':m['node_id'],'generation':m['generation'],'lifecycle_state':m['lifecycle_state'],
                 'clock':None if offsets is None else offsets.view(m['node_id'])}
                for m in coordinator.members()]}
        from .run_model import document as run_model_document
        from .adaptive_owner import adaptive_status, adaptive_publication
        owner_run=_demo_status(self,'owner')
        return {'label':'macOS A13 hackathon demo','role':'not_yet_serving',
                'model_contract':asdict(self.run_model_contract()),
                'run_model':run_model_document(self.root),
                'network':({'state':'private'} if self.public_relay is None else self.public_relay.status()),
                'acquisition':self.acquisition.status(),
                'resource_observation':dict(self.resource_observation),
                'acquisition_policy_error':self.acquisition_policy_error,
                'installation_role':self.installation_role,'resource_policy':read_resource_policy(self.root),
                'running':self.running,'authority':value,'renewal_error':self.renew_error,
                'recovery_error':self.recovery_error,
                'distribution':'unsigned development software; not notarized',
                'leave':self._leave_status(),
                'join_flow':self._join_flow_status(),
                'adaptive_host':adaptive_status(self),'adaptive_host_publication':adaptive_publication(self),
                'sharing':_sharing_status(self),'relay_wait':self._relay_wait_status(),
                'owner_run':owner_run,'demo_target':_demo_status(self,'target'),
                'chat':_demo_status(self,'chat'),'swarm':_swarm_status(self),
                'serving':self._serving_block(owner_run),'peer_liveness':_peer_liveness(),
                'test_seams_active':_test_seam_offset(self) is not None,
                'test_seams':_test_seams.status(_test_seam_offset(self))}

    def _serving_block(self,owner_run=None,*,relay_wait=...):
        wait=self._relay_wait_status() if relay_wait is ... else relay_wait
        if wait is not None:
            return {'state':'waiting_for_relay','route_ready':False,'members_joined':wait['members_joined'],
                    'next_action':'Start the public relay so the joined Macs can serve.'}
        # PV30: follow the owner run and its route readiness (was a constant).
        run=owner_run if isinstance(owner_run,dict) else {}
        state=run.get('state')
        if state=='serving' and run.get('route_ready') is True and run.get('registry_origin'):
            # route_ready is the qualifier's own claim carried by the owner run, never a literal here (claim boundary).
            return {'state':'serving','route_ready':run.get('route_ready') is True,'next_action':'Open the chat.'}
        if state in {'running','serving'}:
            return {'state':'starting','route_ready':False,'next_action':'The route is being prepared.'}
        if state in {'stopping','failing'}:
            return {'state':'stopping','route_ready':False,'next_action':'The run is stopping.'}
        if state=='failed':
            return {'state':'failed','route_ready':False,'error_code':run.get('error_code'),
                    'next_action':'Press Start to run again.'}
        return {'state':'not_yet_serving','route_ready':False,
                'next_action':'Native route preparation and exact model execution approval required.'}

    def action(self, path, body):
        # R26: the consent form may carry a top-level optional ``adaptive_sharing`` bool (ticked by
        # default in the UI).  It is split off before every strict key check and applied only after
        # the consent step succeeded; a body without it behaves exactly as before.
        choice = None
        if path in _CONSENT_ROUTES and isinstance(body, dict) and 'adaptive_sharing' in body:
            choice = body['adaptive_sharing']
            if type(choice) is not bool: raise DemoError('invalid_request')
            body = {k: v for k, v in body.items() if k != 'adaptive_sharing'}
        with self.guard:
            result = DemoApp._action(self, path, body)  # explicit: callers may pass a stand-in ``self``
            if choice is not None:
                from .adaptive_owner import apply_first_consent_choice
                apply_first_consent_choice(self, choice)
            # Publish in the action's existing critical section, before another
            # worker can take the guard. Collection uses snapshots, not monitors;
            # readers remain lock-free while the action/collector does work.
            publisher = getattr(self, 'status_publisher', None)
            if publisher is not None:
                publisher.publish()
        return result

    def test_seams_enabled(self):
        """True only on a joined Mac launched with ``MYCELIUM_A13_TEST_SEAMS=1`` (V1.2 runtime clock seam)."""
        return (self.installation_role == 'target'
                and getattr(self.authority, 'test_seams_enabled', False) is True)

    def _set_test_clock_offset(self, body):
        if not self.test_seams_enabled():
            raise DemoError('test_seams_unavailable')
        if not isinstance(body, dict) or set(body) != {'offset_seconds'}:
            raise DemoError('test_seam_offset_invalid')
        try:
            return self.authority.set_test_clock_offset(body['offset_seconds'])
        except ValueError as error:
            raise DemoError(str(error)) from None

    def _action(self, path, body):
        with self.guard:
            if self.closed.is_set(): raise DemoError('application_closed')
            if self.installation_role == 'target' and path in {'/api/create','/api/revoke','/api/revoke-self','/api/pair/approve','/api/pair/preview',
                    '/api/pair/window/open','/api/pair/window/list','/api/pair/window/approve','/api/pair/window/revoke'}:
                raise DemoError('owner_role_required')
            if path == '/api/leave':
                from .member_leave import leave_action
                return leave_action(self, body)
            if path == '/api/test-seams/clock-offset':
                return self._set_test_clock_offset(body)
            from .adaptive_owner import ADAPTIVE_ROUTES, adaptive_action
            if path in ADAPTIVE_ROUTES:
                return adaptive_action(self, path, body)
            if path == '/api/run-model':
                return self._choose_run_model(body)
            from .demo_app import OWNER_ROUTES as _DEMO_ROUTES
            if path in _DEMO_ROUTES:
                from .demo_app import action as _demo_action
                return _demo_action(self, path, body)
            if path.startswith('/api/swarm/'):
                from .swarm_publish import action as _swarm_action
                return _swarm_action(self, path, body)
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
                if path in {'/api/serving/activate','/api/serving/start'}:
                    from .demo_app import relay_gate
                    relay_gate(self)
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
                _base={'relay_acknowledged','max_seconds'}
                external=body.get('provider')=='external'
                if (self.installation_role!='owner' or not self.authority.status().get('swarm_created')
                    or not (set(body)==_base or (external and _base|{'provider'}<=set(body)<=_base|_EXTERNAL_OPTIONAL))
                    or body['relay_acknowledged'] is not True
                    or type(body['max_seconds']) is not int or not 30<=body['max_seconds']<=1800):
                    raise DemoError('network_consent_required')
                spec={**self._saved_external_address(),**body} if external else body
                if getattr(self,'local_contributor',None) is not None and not self._external_origin_unchanged(spec):
                    raise DemoError('network_stop_local_contributor_required')
                from .endpoint_recovery import relocate_authority
                if external and not {'public_origin','proxy_port'}<=set(spec):raise DemoError('relay_origin_invalid')
                fresh=self._new_relay(self.root,spec) if external else None  # validated before the old relay is touched
                if self.public_relay is not None:
                    self.public_relay.close();self.public_relay=None
                if fresh is None:
                    from .public_relay import TemporaryHttpsRelay
                    fresh=TemporaryHttpsRelay(self.root)
                relay=fresh.start(acknowledged=True,max_seconds=body['max_seconds'])
                try:
                    port=self._free_seed_port(avoid=getattr(relay,'_proxy_port',None))
                    if external and self.authority._network_configuration()['public_origin']==relay.public_origin:
                        card=None  # the address did not change: members need no recovery card
                    else:
                        card=relocate_authority(self.authority,public_origin=relay.public_origin,port=port,acknowledged=True)
                    relay.proxy.bind_seed(self.authority.seed_server)
                except BaseException:
                    relay.close();raise
                self.public_relay=relay;self.running=True
                if external:self._remember_external_address(spec)
                self._bind_public_routes()
                return {'network':relay.status(),'recovery_card':card}
            if path == '/api/network/start':
                return self._start_public_relay(body)
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
                self._become_target(body['consent'])
                return self.authority.pair_request()
            if path in {'/api/pair/window/open','/api/pair/window/list','/api/pair/window/approve','/api/pair/window/revoke'}:
                return self._owner_window_action(path, body)
            if path in {'/api/join/window','/api/join/window/poll','/api/join/window/cancel','/api/consent/standing/withdraw'}:
                return self._target_join_action(path, body)
            if path == '/api/create':
                with_relay = set(body) == {'consent','relay_acknowledged','max_seconds'}
                if (set(body) != {'consent'} and not with_relay) or not consent_valid(body['consent']):
                    raise DemoError('consent_required')
                started = None
                if with_relay:
                    # Right order, automatically: the relay precedes the swarm.
                    started = self._start_public_relay(
                        {'relay_acknowledged':body['relay_acknowledged'],'max_seconds':body['max_seconds']})
                try:
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
                except BaseException:
                    if started is not None and self.public_relay is not None:
                        self.public_relay.close();self.public_relay=None
                    raise
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
                # Reconnect can be clicked while this member is still running.
                # The owner's in-memory report inbox may have restarted alone.
                if self.installation_role == 'target' and self.authority.peer.membership_binding() is not None:
                    self._auto_report('periodic')
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
                result = self.authority.report_capability()
                if self.installation_role == 'target':
                    result = {**result, 'owner_report':self._auto_report('periodic', seed_capability=result)}
                return result
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

    def _owner_window_action(self, path, body):
        from .pairing import PairingError
        if path == '/api/pair/window/list':
            if body: raise DemoError('invalid_request')
            return {'windows':self.pairing_windows.list()}
        if path == '/api/pair/window/revoke':
            if set(body) != {'window_id'}: raise DemoError('invalid_request')
            for item in list(self._pending_windows):
                if item['pending_id'] == body['window_id']:
                    self._pending_windows.remove(item)
                    return {'window_id':item['pending_id'],'state':'revoked'}
            return self.pairing_windows.revoke(body['window_id'])
        if path == '/api/pair/window/open':
            if set(body) != {'ttl_seconds'}: raise DemoError('invalid_request')
            current = self.authority.status()
            if not current.get('swarm_created') or self.authority.coordinator is None:
                raise DemoError('pairing_window_swarm_required')
            relay = self.public_relay
            origin = None if relay is None else relay.status().get('public_origin')
            if relay is None or not origin:
                # Relay-only by design: venue Wi-Fi isolates clients, so a target can reach the
                # owner only through the public relay.  The code can still be asked for now; it
                # becomes redeemable when the relay starts (_materialize_pending_windows).
                return self._open_pending_window(body['ttl_seconds'])
            self._bind_public_routes()
            if self.authority.coordinator.seed_url != origin:
                raise DemoError('pairing_window_origin_stale')
            return self.pairing_windows.open(origin=origin,
                seed_key_digest=self.authority.coordinator.signer.verification_key_digest, ttl_seconds=body['ttl_seconds'])
        if set(body) != {'window_id','request_digest'}: raise DemoError('invalid_request')
        def issue(request, quota):
            if not pairing_quota_valid(quota):
                raise PairingError('pairing_quota_invalid')
            remaining = int(float(request['expires_at']) - time.time()) - 1
            if remaining < 30: raise PairingError('pairing_request_expiring')
            return self.authority.approve_pairing(request, ttl_seconds=min(120, remaining), quota=quota)
        return self.pairing_windows.approve(body['window_id'], body['request_digest'], issue)

    def _target_join_action(self, path, body):
        from .pairing import PairingError, identity_fingerprint, parse_join_text, request_digest
        from .standing_consent import (bind_standing_consent, grant_standing_consent,
                                       withdraw_standing_consent)
        from .authority import _atomic_private_json
        if path == '/api/consent/standing/withdraw':
            if set(body) != {'confirm'} or body['confirm'] is not True:
                raise DemoError('revoke_confirmation_required')
            if self.installation_role == 'target' and self.authority is not None:
                self.authority.stop_control()
            value = withdraw_standing_consent(self.root, now=time.time())
            if value is None: raise DemoError('standing_consent_missing')
            return {'state':value['state'],'withdrawn_at':value['withdrawn_at'],'route_ready':False}
        if path == '/api/join/window/cancel':
            if body: raise DemoError('invalid_request')
            self._join_state_path().unlink(missing_ok=True)
            return {'state':'cancelled'}
        if path == '/api/join/window':
            if (set(body) != {'consent','standing_consent','join'} or not consent_valid(body['consent'])):
                raise DemoError('consent_required')
            if self.installation_role != 'target' and self.authority.status().get('swarm_created'):
                raise DemoError('owner_cannot_become_target')
            if self.installation_role == 'target' and self.authority.status().get('membership_state') == 'current':
                raise DemoError('already_joined')
            link = parse_join_text(body['join'], now=time.time())
            # Validate the standing grant against THIS consent before any role change.
            from .standing_consent import _caps, StandingConsentError
            sc = body['standing_consent']
            if not isinstance(sc, dict) or set(sc) != {'acknowledged','disclosure_digest','caps'}:
                raise DemoError('standing_consent_required')
            device_memory = self._device_memory_bytes()
            try: _caps(sc['caps'], body['consent'], device_memory_bytes=device_memory)
            except StandingConsentError as error: raise DemoError(error.code) from None
            self._become_target(body['consent'])
            try: grant_standing_consent(self.root, sc, policy=body['consent'], now=time.time(),
                                        device_memory_bytes=device_memory)
            except StandingConsentError as error: raise DemoError(error.code) from None
            request = self.authority.pair_request()
            client = self._join_client(link['origin'])
            report = self._measure_and_sign(client, 'join')
            storage = body['consent']['storage_bytes']
            result = client.submit(link['window_id'], code=link['code'], request=request, report=report,
                quota={'storage_bytes':storage,'network_bytes_per_day':storage,'background_allowed':False})
            fingerprint = identity_fingerprint(request['identity_key_digest'])
            if result.get('state') != 'waiting_for_owner' or result.get('fingerprint') != fingerprint:
                raise DemoError('pairing_window_response_invalid')
            _atomic_private_json(self._join_state_path(), {
                'protocol':'mycelium.a13_join_window_state.v1','origin':link['origin'],
                'window_id':link['window_id'],'code':link['code'],'seed_key_digest':link['seed_key_digest'],
                'request_digest':request_digest(request),'fingerprint':fingerprint})
            joined_view = {'state':'waiting_for_owner','fingerprint':fingerprint,
                           'pinned':link['seed_key_digest'] is not None}
            if client.warnings: joined_view['warnings'] = list(client.warnings)  # advisory (clock_skew)
            return joined_view
        # /api/join/window/poll
        if body: raise DemoError('invalid_request')
        state = self._load_join_state()
        if state is None or self.installation_role != 'target': raise DemoError('join_window_missing')
        try:
            value = self._join_client(state['origin']).poll(state['window_id'], code=state['code'],
                                                            digest=state['request_digest'])
        except PairingError as error:
            if error.code in {'pairing_window_unknown','pairing_window_expired','pairing_window_revoked',
                              'pairing_window_locked','pairing_window_request_mismatch'}:
                self._join_state_path().unlink(missing_ok=True)
            raise
        if value.get('state') == 'waiting_for_owner':
            return {'state':'waiting_for_owner','fingerprint':state['fingerprint']}
        if value.get('state') != 'approved' or not isinstance(value.get('handoff'), dict):
            raise DemoError('pairing_window_response_invalid')
        handoff = value['handoff']
        if (state['seed_key_digest'] is not None
                and self.authority.peer.handoff_seed_key_digest(handoff) != state['seed_key_digest']):
            raise DemoError('join_seed_pin_mismatch')
        joined = self.authority.accept_pairing(handoff)
        binding = self.authority.peer.membership_binding()
        if binding is not None:
            try: bind_standing_consent(self.root, binding, now=time.time())
            except Exception as error:
                from .control_failures import error_http
                self.standing_error = public_failure_code(error)
                self.standing_error_http = error_http(error, self.standing_error)
        self._join_state_path().unlink(missing_ok=True)
        self.running = True
        report = self._auto_report('join')
        return {'state':'joined','membership_state':joined.get('membership_state', 'current')
                if isinstance(joined, dict) else 'current',
                'membership_generation':self.authority.member.generation,
                'report':report,'route_ready':False}

    def close(self):
        publisher = self.status_publisher
        if publisher is not None: publisher.stop()
        # Stop the one-click run first, outside the guard: its teardown drives
        # this Mac's own contributor through app.action (which takes the guard).
        if not self.closed.is_set():
            try:
                from .demo_app import close as _demo_close
                _demo_close(self)
            except Exception:
                self.cleanup_error='demo_run_stop_failed'
            try:
                from .swarm_publish import close as _swarm_close
                _swarm_close(self)
            except Exception:
                pass
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
        if self.path in {'/','/app.js','/style.css','/run.js','/swarm.js'}:
            name={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/run.js':'run.js','/swarm.js':'swarm.js'}[self.path]
            path=Path(__file__).parent/'web'/name
            if not path.is_file(): self.fail(503,'assets_unavailable'); return
            kind={'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/run.js':'text/javascript; charset=utf-8','/swarm.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}[self.path]
            self.send_value(200,path.read_bytes(),content_type=kind); return
        if self.path == '/chat' or self.path.startswith('/chat/'):
            self.chat_route(b''); return
        if self.path.startswith('/api/pair/window/qr/') and self.path.endswith('.svg'):
            # Owner-only QR of an OPEN pairing window's HTTPS join page.
            if not self.authorized(): return
            import re as _re
            window_id=self.path[len('/api/pair/window/qr/'):-4]
            if self.app.installation_role!='owner' or not _re.fullmatch(r'[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}',window_id):
                self.fail(404,'not_found'); return
            from .join_page import owner_qr_svg
            svg=owner_qr_svg(self.app.pairing_windows,window_id)
            if svg is None: self.fail(410,'pairing_window_closed'); return
            self.send_value(200,svg,content_type='image/svg+xml'); return
        if self.path == '/api/status':
            if not self.authorized(): return
            try: self.send_value(200,self.app.read_status())
            except Exception: self.fail(503,'status_unavailable')
            return
        self.fail(404,'not_found')
    def chat_route(self, body):
        # Shared chat page: no accounts and no session cookie (local UI and the
        # same handler behind the public relay); limits live in chat.py.
        from .chat import handle, CSP
        try:
            status,kind,raw=handle(self.app,self.command,self.path,
                {k:v for k,v in self.headers.items()},body,via_relay=False)
        except Exception:
            self.fail(503,'chat_unavailable'); return
        self.send_value(status,raw,content_type=kind,extra={'Content-Security-Policy':CSP})
    def do_POST(self):
        if not self.base_guard(): return
        if self.path == '/api/test-seams/clock-offset':
            # V1.2 runtime clock seam: the route exists only on a joined Mac launched with the gate open, loopback only.
            if not self.app.test_seams_enabled(): self.fail(404,'not_found'); return
            if self.client_address[0] not in {'127.0.0.1','::1'}: self.fail(403,'loopback_only'); return
        if self.path.startswith('/chat/'):
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[])) != 1:
                self.fail(400,'framing_rejected'); return
            try:
                length=int(self.headers['Content-Length'])
                if not 0<=length<=MAX_BODY: raise ValueError
                raw=self.rfile.read(length)
            except (ValueError,TypeError,OSError): self.fail(400,'framing_rejected'); return
            self.chat_route(raw); return
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
        except DemoError as e: self.send_value(400,action_error_body(e)); return
        except Exception as error:
            code=public_failure_code(error)
            # Record WHY before answering. The public body stays a closed code;
            # this private one-line identity is what makes the next failure
            # self-describing instead of another generic 409 investigation.
            record=None
            try:
                record=bounded_action_failure(self.path,error,code)
                private_write(self.app.root/'last-action-error.private.json',
                    json.dumps(record,sort_keys=True).encode())
            except BaseException:
                pass
            # A seed window refusal also names its numbers in the daemon log
            # (the app's stdout): two-Mac run 19's 409 left only the code.
            try:
                event=seed_time_refusal_event(self.path,code,record)
                if event is not None:
                    print(json.dumps(event,sort_keys=True),flush=True)
            except BaseException:
                pass
            self.fail(409,code); return
        self.send_value(200,result)
