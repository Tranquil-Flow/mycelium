# SPDX-License-Identifier: AGPL-3.0-only
"""Target-wide durable sequence allocation; never resets a signer on a new run."""
try:  # POSIX lock; win32 goes through mycelium_host_platform.
    import fcntl
except ImportError:  # pragma: no cover - win32
    fcntl = None
import os
from pathlib import Path
import stat

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError

from .authority import _private_directory, _atomic_private_json, _load_private_json


def next_target_sequence(target_root, *, signer_key_digest, swarm_id, at_least):
    root=_private_directory(Path(target_root),create=False)
    if type(at_least) is not int or at_least<0:
        raise ValueError('control_sequence_invalid')
    if mycelium_host_platform.is_win32():
        try:
            lock=mycelium_host_platform.hold_exclusive_lock(root,'control-sequence.lock')
        except HostPlatformError:
            raise ValueError('control_sequence_invalid') from None
        try:
            return _next_locked(root,signer_key_digest=signer_key_digest,swarm_id=swarm_id,at_least=at_least)
        finally:
            lock.release()
    fd=os.open(root/'control-sequence.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600):
            raise ValueError('control_sequence_invalid')
        fcntl.flock(fd,fcntl.LOCK_EX)
        return _next_locked(root,signer_key_digest=signer_key_digest,swarm_id=swarm_id,at_least=at_least)
    finally:
        os.close(fd)


def _next_locked(root, *, signer_key_digest, swarm_id, at_least):
    path=root/'control-sequence.json'
    value=_load_private_json(path)
    scope={'protocol':'mycelium.a13.target_control_sequence.v1',
           'signer_key_digest':signer_key_digest,'swarm_id':swarm_id}
    if value is None:
        # Upgrade from per-run counters without deleting any retained run.
        # Only this installation's private, signer-bound pump state counts.
        floor=at_least
        for prior in (root/'managed').glob('*/pump/state.json'):
            state=_load_private_json(prior)
            if not isinstance(state,dict) or not isinstance(state.get('binding'),dict):
                raise ValueError('control_sequence_legacy_invalid')
            if state['binding'].get('target_key_digest')!=signer_key_digest:
                continue
            counter=state.get('sequence')
            if type(counter) is not int or counter<0:
                raise ValueError('control_sequence_legacy_invalid')
            floor=max(floor,counter)
        value={**scope,'sequence':floor}
    if (set(value)!={*scope,'sequence'}
            or any(value[key]!=item for key,item in scope.items())
            or type(value['sequence']) is not int or value['sequence']<0):
        raise ValueError('control_sequence_scope_invalid')
    value['sequence']=max(value['sequence'],at_least)+1
    _atomic_private_json(path,value)
    return value['sequence']
