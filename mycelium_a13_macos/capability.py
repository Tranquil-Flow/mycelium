# SPDX-License-Identifier: AGPL-3.0-only
"""Measured OS resources, signed by the member; NEVER execution qualification."""
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import shutil
import subprocess
import threading
import time


# Native initialization can register types before raising. CPython removes the
# failed module, but nanobind retains those registrations; retrying then aborts
# the entire app. Join has two capability probes, so retain an import failure
# for this process lifetime (not arithmetic failures after a successful import).
_MLX_IMPORT_LOCK = threading.Lock()
_MLX_IMPORT_ERROR = None


def runtime_probe_report():
    """Explicit tiny arithmetic only; no identity, model, or qualification."""
    global _MLX_IMPORT_ERROR
    report={'protocol':'mycelium.a13_runtime_probe.v1','backend':'mlx',
        'dtype':'float32','available':False,'model_loaded':False}
    with _MLX_IMPORT_LOCK:
        if _MLX_IMPORT_ERROR is not None:
            report['error_type'] = _MLX_IMPORT_ERROR
            return report
        try:
            import mlx.core as mx
        except (ImportError,RuntimeError,OSError,ValueError) as exc:
            _MLX_IMPORT_ERROR = type(exc).__name__
            report['error_type'] = _MLX_IMPORT_ERROR
            return report
    try:
        value=mx.array([1.0],dtype=mx.float32)
        result=value+value
        mx.eval(result)
        report.update(value=result.item(),device=str(mx.default_device()))
        report['available']=result.dtype==mx.float32 and report['value']==2.0
    except (ImportError,RuntimeError,OSError,ValueError) as exc:
        report['error_type']=type(exc).__name__
    return report


def probe_mlx():
    """One float32 arithmetic operation, not a model or serving challenge."""
    return runtime_probe_report()['available']


def observe_mac(root, *, system=None, architecture=None):
    system=platform.system() if system is None else system
    architecture=platform.machine() if architecture is None else architecture
    if system!='Darwin' or architecture!='arm64': raise ValueError('unsupported_mac')
    result=subprocess.run(['/usr/sbin/sysctl','-n','hw.memsize'],capture_output=True,
                          text=True,check=True,timeout=3)
    memory=int(result.stdout.strip())
    available=shutil.disk_usage(Path(root)).free
    if memory<=0 or available<0: raise ValueError('mac_resources_unavailable')
    # Installation alone is insufficient. A tiny actual operation establishes
    # only self-reported runtime availability, never model/physical qualification.
    available_backend=probe_mlx()
    return {'platform':'macos','architecture':'arm64','memory_bytes':memory,
            'available_storage_bytes':available,
            'backends':['mlx'] if available_backend else [],
            'precisions':['float32'] if available_backend else [],
            'runtime_evidence':'local_arithmetic_self_probe' if available_backend else 'unavailable',
            'independently_qualified':False}


def signed_mac_report(member,root):
    if member is None or member.generation is None: raise ValueError('membership_required')
    value=observe_mac(root)
    if not value['backends'] or not value['precisions']:
        raise ValueError('capability_backend_unavailable')
    return member.capability_report(**{k:value[k] for k in (
        'platform','architecture','memory_bytes','available_storage_bytes','backends','precisions')})



# -- automatic capability + relay-link report (A13 join flow) -----------------
#
# A target reports this on join (inside its pairing-window submission), before
# each automatically accepted run, and periodically while it is a member.  It
# is signed by the target's durable identity key and is a SELF-REPORT: it never
# qualifies a model, an assignment or a route.  The owner keeps the latest
# verified report per member (``MemberReports``) for planning; the compile
# lane may read it through ``DemoApp.member_capability_reports()``.

CAPABILITY_LINK_REPORT_PROTOCOL='mycelium.a13_capability_link_report.v1'
LINK_PROBE_PATH='/link-probe/v1'
MEMBER_REPORT_PATH='/member-report/v1'
REPORT_TTL_SECONDS=600
_REPORT_PURPOSES=frozenset({'join','pre_run','periodic'})
_DEVICE_FIELDS=frozenset({'chip','memory_bytes','available_storage_bytes','os_version',
    'mlx','power','thermal','network'})
_LINK_FIELDS=frozenset({'path_kind','rounds','rtt_ms_median','rtt_ms_samples','upload_bytes',
    'upload_bytes_per_second','download_bytes','download_bytes_per_second'})
_REPORT_FIELDS=frozenset({'protocol','node_id','identity_key_digest','identity_verification_key',
    'purpose','generation','observed_at','expires_at','device','link',
    'independently_qualified','signature'})
_TEXT=re.compile(r'[ -~]{1,96}\Z')
_NODE=re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
_POWER={'external','battery','unknown'}
_THERMAL={'nominal','fair','serious','critical','unknown'}
_NETWORK={'unmetered','metered','unavailable','unknown'}
_MAX_PROBE_KIB=256


def _report_error(code):
    from .pairing import PairingError
    return PairingError(code)


def _sysctl_text(run,name):
    try:
        value=run(['/usr/sbin/sysctl','-n',name],capture_output=True,text=True,check=True,
                  timeout=3).stdout.strip()
    except (OSError,subprocess.SubprocessError,ValueError):
        return None
    return value if _TEXT.fullmatch(value or '') else None


def observe_device(root,*,system=None,architecture=None,run=subprocess.run,os_version=None,
                   probe=None,observe_policy=None):
    """Chip, RAM, free disk, MLX probe and power/thermal; unknown is never invented."""
    system=platform.system() if system is None else system
    architecture=platform.machine() if architecture is None else architecture
    if system!='Darwin' or architecture!='arm64':raise ValueError('unsupported_mac')
    memory_text=_sysctl_text(run,'hw.memsize')
    memory=int(memory_text) if memory_text and memory_text.isdigit() else 0
    if memory<=0:raise ValueError('mac_resources_unavailable')
    chip=_sysctl_text(run,'machdep.cpu.brand_string') or 'unknown'
    version=platform.mac_ver()[0] if os_version is None else os_version
    if not isinstance(version,str) or not _TEXT.fullmatch(version):version='unknown'
    try:runtime=(probe or runtime_probe_report)()
    except Exception:runtime={'available':False}
    device=runtime.get('device') if runtime.get('available') is True else None
    mlx={'available':runtime.get('available') is True,
         'device':device if isinstance(device,str) and _TEXT.fullmatch(device) else None}
    try:
        if observe_policy is None:
            from .resources import observe_policy
        policy=observe_policy()
    except Exception:
        policy={}
    if not isinstance(policy,dict):policy={}
    return {'chip':chip,'memory_bytes':memory,
            'available_storage_bytes':int(shutil.disk_usage(Path(root)).free),
            'os_version':version,'mlx':mlx,
            'power':policy.get('power') if policy.get('power') in _POWER else 'unknown',
            'thermal':policy.get('thermal') if policy.get('thermal') in _THERMAL else 'unknown',
            'network':policy.get('network') if policy.get('network') in _NETWORK else 'unknown'}


def measure_link(transport,*,rounds=3,bulk_bytes=_MAX_PROBE_KIB*1024,clock=time.perf_counter):
    """RTT and throughput over the SAME relay path the member uses to reach the owner.

    ``transport(download_kib, upload_bytes) -> raw response`` posts to the
    owner's ``/link-probe/v1/<download_kib>`` route through the public relay.
    """
    if (type(rounds) is not int or not 1<=rounds<=8 or type(bulk_bytes) is not int
            or bulk_bytes%1024 or not 1024<=bulk_bytes<=_MAX_PROBE_KIB*1024):
        raise _report_error('link_probe_invalid')
    def timed(download_kib,upload):
        started=clock()
        raw=transport(download_kib,upload)
        elapsed=clock()-started
        try:value=json.loads(raw)
        except (TypeError,ValueError):raise _report_error('link_probe_invalid') from None
        if (not isinstance(value,dict) or value.get('received_bytes')!=len(upload)
                or not isinstance(value.get('filler'),str)
                or len(value['filler'])!=download_kib*1024 or not elapsed>0):
            raise _report_error('link_probe_invalid')
        return elapsed
    samples=[round(timed(0,b'')*1000,1) for _ in range(rounds)]
    upload=timed(0,b'\0'*bulk_bytes)
    download=timed(bulk_bytes//1024,b'')
    ordered=sorted(samples)
    return {'path_kind':'public_https_relay','rounds':rounds,
            'rtt_ms_median':ordered[len(ordered)//2],'rtt_ms_samples':samples,
            'upload_bytes':bulk_bytes,'upload_bytes_per_second':round(bulk_bytes/upload,1),
            'download_bytes':bulk_bytes,'download_bytes_per_second':round(bulk_bytes/download,1)}


def link_probe_response(download_kib,body):
    """Owner side of ``/link-probe/v1/<kib>``: bounded, stateless, no secrets."""
    if type(download_kib) is not int or not 0<=download_kib<=_MAX_PROBE_KIB:
        raise _report_error('link_probe_invalid')
    if len(body)>_MAX_PROBE_KIB*1024:raise _report_error('link_probe_invalid')
    return json.dumps({'received_bytes':len(body),'filler':'A'*(download_kib*1024)},
                      separators=(',',':')).encode()


def _finite_number(value,*,minimum=0.0):
    return (not isinstance(value,bool) and isinstance(value,(int,float))
            and math.isfinite(float(value)) and float(value)>=minimum)


def _device_valid(device):
    return (isinstance(device,dict) and set(device)==_DEVICE_FIELDS
        and isinstance(device['chip'],str) and _TEXT.fullmatch(device['chip'])
        and isinstance(device['os_version'],str) and _TEXT.fullmatch(device['os_version'])
        and type(device['memory_bytes']) is int and device['memory_bytes']>0
        and type(device['available_storage_bytes']) is int and device['available_storage_bytes']>=0
        and isinstance(device['mlx'],dict) and set(device['mlx'])=={'available','device'}
        and type(device['mlx']['available']) is bool
        and (device['mlx']['device'] is None or (isinstance(device['mlx']['device'],str)
             and _TEXT.fullmatch(device['mlx']['device'])))
        and device['power'] in _POWER and device['thermal'] in _THERMAL
        and device['network'] in _NETWORK)


def _link_valid(link):
    return (isinstance(link,dict) and set(link)==_LINK_FIELDS
        and link['path_kind']=='public_https_relay'
        and type(link['rounds']) is int and 1<=link['rounds']<=8
        and isinstance(link['rtt_ms_samples'],list) and len(link['rtt_ms_samples'])==link['rounds']
        and all(_finite_number(v) for v in link['rtt_ms_samples'])
        and _finite_number(link['rtt_ms_median'])
        and type(link['upload_bytes']) is int and type(link['download_bytes']) is int
        and _finite_number(link['upload_bytes_per_second'])
        and _finite_number(link['download_bytes_per_second']))


def _caps_valid(caps):
    return (isinstance(caps, dict) and bool(caps)
            and set(caps) <= {'storage_bytes', 'max_memory_bytes'}
            and all(type(v) is int and 0 < v <= 2**63 - 1 for v in caps.values()))


def signed_capability_link_report(*,signer,node_id,purpose,generation,device,link,now,
                                  ttl_seconds=REPORT_TTL_SECONDS,caps=None):
    if (purpose not in _REPORT_PURPOSES or not isinstance(node_id,str) or not _NODE.fullmatch(node_id)
            or not (generation is None or (type(generation) is int and generation>=0))
            or (purpose!='join' and generation is None)
            or not _device_valid(device) or not _link_valid(link) or not _finite_number(now)
            or (caps is not None and not _caps_valid(caps))
            or type(ttl_seconds) is not int or not 1<=ttl_seconds<=REPORT_TTL_SECONDS):
        raise _report_error('capability_report_invalid')
    statement={'protocol':CAPABILITY_LINK_REPORT_PROTOCOL,'node_id':node_id,
        'identity_key_digest':signer.verification_key_digest,
        'identity_verification_key':signer.public_key_record(),'purpose':purpose,
        'generation':generation,'observed_at':float(now),'expires_at':float(now)+ttl_seconds,
        'device':json.loads(json.dumps(device)),'link':json.loads(json.dumps(link)),
        'independently_qualified':False}
    if caps is not None:
        statement['caps'] = dict(caps)
    return {**statement,'signature':signer.sign(statement)}


def verify_capability_link_report(report,*,now,expected_key_digest,node_id,purposes=_REPORT_PURPOSES):
    from mycelium_qualification.evidence import canonical_json_bytes
    from mycelium_qualification.signing import build_ed25519_verifier
    if (not isinstance(report,dict) or set(report) not in (_REPORT_FIELDS, _REPORT_FIELDS | {'caps'})
            or report.get('protocol')!=CAPABILITY_LINK_REPORT_PROTOCOL):
        raise _report_error('capability_report_invalid')
    statement={k:v for k,v in report.items() if k!='signature'}
    record=report.get('identity_verification_key')
    try:
        if not isinstance(record,dict) or record.get('verification_key_digest')!=expected_key_digest:
            raise ValueError
        verify=build_ed25519_verifier([dict(record)])
        valid=(report.get('identity_key_digest')==expected_key_digest
               and isinstance(report.get('signature'),dict)
               and verify(canonical_json_bytes(statement),report['signature']))
    except Exception:
        valid=False
    if not valid or report.get('node_id')!=node_id:
        raise _report_error('capability_report_signature_invalid')
    if (report['purpose'] not in purposes or report['independently_qualified'] is not False
            or not _device_valid(report['device']) or not _link_valid(report['link'])
            or ('caps' in report and not _caps_valid(report['caps']))
            or not _finite_number(report['observed_at']) or not _finite_number(report['expires_at'])
            or not 0<report['expires_at']-report['observed_at']<=REPORT_TTL_SECONDS):
        raise _report_error('capability_report_invalid')
    if not _finite_number(now) or now>report['expires_at'] or now+60<report['observed_at']:
        raise _report_error('capability_report_expired')
    return statement


def _json_reply(status,value):
    return status,json.dumps(value,separators=(',',':')).encode()


class MemberReports:
    """Owner inbox: the latest verified report of each CURRENT member only.

    ``lookup(node_id)`` returns the seed's view ``{verification_key_digest,
    generation, lifecycle_state}`` or ``None``.
    """
    _CURRENT=frozenset({'NEW','CONFIGURED','RUNNING'})  # seed lifecycle, not STOPPING/STOPPED
    _MAX_MEMBERS=64

    def __init__(self,*,lookup,clock=time.time):
        self._lookup=lookup;self._clock=clock
        self._lock=threading.Lock();self._latest={}

    def submit(self,report):
        node_id=report.get('node_id') if isinstance(report,dict) else None
        if not isinstance(node_id,str) or not _NODE.fullmatch(node_id):
            raise _report_error('capability_report_invalid')
        member=self._lookup(node_id)
        if member is None:raise _report_error('capability_report_member_not_current')
        statement=verify_capability_link_report(report,now=self._clock(),
            expected_key_digest=member['verification_key_digest'],node_id=node_id)
        if (statement['generation']!=member['generation']
                or member['lifecycle_state'] not in self._CURRENT):
            raise _report_error('capability_report_member_not_current')
        with self._lock:
            if node_id not in self._latest and len(self._latest)>=self._MAX_MEMBERS:
                raise _report_error('capability_report_capacity')
            previous=self._latest.get(node_id)
            if previous is None or previous['observed_at']<=statement['observed_at']:
                self._latest[node_id]={k:statement[k] for k in (
                    'purpose','generation','observed_at','expires_at','device','link')}
                if 'caps' in statement:
                    self._latest[node_id]['caps'] = dict(statement['caps'])
        return {'state':'accepted','node_id':node_id}

    def latest(self):
        with self._lock:
            return json.loads(json.dumps(self._latest))

    def handle(self,method,path,body):
        if path!=MEMBER_REPORT_PATH:return _json_reply(404,{'error':'route_unknown'})
        if method!='POST':return _json_reply(405,{'error':'method_not_allowed'})
        try:
            report=json.loads(body)
            if not isinstance(report,dict):raise ValueError
        except (TypeError,ValueError,UnicodeError):
            return _json_reply(400,{'error':'capability_report_invalid'})
        try:return _json_reply(200,self.submit(report))
        except Exception as exc:
            code=getattr(exc,'code',None)
            if not isinstance(code,str) or not code.startswith('capability_report_'):
                code='capability_report_invalid'
            status=409 if code in {'capability_report_member_not_current','capability_report_expired',
                                   'capability_report_capacity'} else 400
            return _json_reply(status,{'error':code})
