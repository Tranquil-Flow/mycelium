"""Target-wide durable sequence allocation; never resets a signer on a new run."""
import fcntl
import os
from pathlib import Path
import stat

from .authority import _private_directory, _atomic_private_json, _load_private_json


def next_target_sequence(target_root, *, signer_key_digest, swarm_id, at_least):
    root=_private_directory(Path(target_root),create=False)
    if type(at_least) is not int or at_least<0:
        raise ValueError('control_sequence_invalid')
    fd=os.open(root/'control-sequence.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600):
            raise ValueError('control_sequence_invalid')
        fcntl.flock(fd,fcntl.LOCK_EX)
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
    finally:
        os.close(fd)
