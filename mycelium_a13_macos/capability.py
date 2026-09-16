# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measured OS resources, signed by the member; NEVER execution qualification."""
from pathlib import Path
import platform
import shutil
import subprocess


def runtime_probe_report():
    """Explicit tiny arithmetic only; no identity, model, or qualification."""
    report={'protocol':'mycelium.a13_runtime_probe.v1','backend':'mlx',
        'dtype':'float32','available':False,'model_loaded':False}
    try:
        import mlx.core as mx
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
