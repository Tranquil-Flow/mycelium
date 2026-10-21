# SPDX-License-Identifier: AGPL-3.0-only
"""Target-owned, bounded adapter to canonical controller staging/cleanup.

The target chooses roots and authorizes exact content before local execution.
No script, argv, executable or filesystem root is accepted from a remote job.
The only executed staging code is the installed canonical controller script.
These operations do not load tensors, launch a worker, or qualify a route.
"""
from __future__ import annotations
from contextlib import contextmanager
try:  # POSIX target staging; Windows uses mycelium_a13_windows.managed_host.
    import fcntl
except ImportError:  # pragma: no cover - win32
    fcntl = None
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile

from .authority import _private_directory


class StagingError(ValueError):
    pass


def _digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def _pin(value):
    if not isinstance(value, str) or re.fullmatch(r'sha256:[a-f0-9]{64}', value) is None:
        raise StagingError('stage_digest_invalid')
    return value


def _relative(value):
    if not isinstance(value, str):
        raise StagingError('stage_path_invalid')
    path=PurePosixPath(value)
    if (not value or path.is_absolute() or str(path)!=value or
        any(part in ('.','..') or part.startswith('.mycelium') or part=='.incoming.tar' for part in path.parts)):
        raise StagingError('stage_path_invalid')
    return path


def _regular(path):
    current=Path(path.anchor)
    for part in path.parts[1:]:
        current=current/part
        if current.is_symlink():
            raise StagingError('stage_file_unsafe')
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid():
        raise StagingError('stage_file_unsafe')
    return info


def _marker(path):
    try:
        info=_regular(path)
        if stat.S_IMODE(info.st_mode)!=0o600 or not 0<info.st_size<=1024:
            raise StagingError('stage_marker_invalid')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            opened=os.fstat(fd)
            if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino):
                raise StagingError('stage_marker_invalid')
            raw=os.read(fd,1025)
        finally: os.close(fd)
        value=json.loads(raw)
        if not isinstance(value,dict) or _canonical(value)!=raw:
            raise StagingError('stage_marker_invalid')
        return value
    except (OSError,ValueError,UnicodeError):
        raise StagingError('stage_marker_invalid') from None


class TargetStaging:
    def __init__(self, state_root, *, node_id, max_archive_bytes, max_disk_bytes,
                 current_member, authorize, artifact_cache_root=None):
        if (not isinstance(node_id,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',node_id) is None
            or any(type(n) is not int or n<=0 for n in (max_archive_bytes,max_disk_bytes))
            or max_archive_bytes>64*1024*1024 or not callable(current_member) or not callable(authorize)):
            raise StagingError('stage_configuration_invalid')
        self.root=_private_directory(Path(state_root),create=True)
        self.staging_root=self.root/'mycelium-stage'
        self.node_id=node_id
        self.max_archive_bytes=max_archive_bytes
        self.max_disk_bytes=max_disk_bytes
        self.current_member=current_member
        self.authorize=authorize
        self.cache=None if artifact_cache_root is None else Path(artifact_cache_root).absolute()
        if self.cache is not None and (self.cache.is_symlink() or not self.cache.is_dir() or self.cache.resolve()!=self.cache):
            raise StagingError('stage_cache_invalid')

    @contextmanager
    def _lock(self):
        fd=os.open(self.root/'stage.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600:
                raise StagingError('stage_lock_invalid')
            try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise StagingError('stage_busy') from None
            yield
        finally:
            os.close(fd)

    def _admit(self,intent):
        try:
            accepted=self.current_member() is True and self.authorize(dict(intent)) is True
        except Exception: accepted=False
        if not accepted: raise StagingError('stage_not_authorized')

    def _files(self, archive, preposition):
        files={}
        try:
            with tarfile.open(fileobj=io.BytesIO(archive),mode='r:') as tar:
                members=tar.getmembers()
                names=[m.name for m in members]
                if not members or len(members)>256 or names!=sorted(set(names)):
                    raise StagingError('stage_archive_invalid')
                for member in members:
                    _relative(member.name)
                    if not member.isfile() or member.size<0 or member.size>self.max_disk_bytes:
                        raise StagingError('stage_archive_invalid')
                    handle=tar.extractfile(member)
                    if handle is None: raise StagingError('stage_archive_invalid')
                    data=handle.read(member.size+1)
                    if len(data)!=member.size: raise StagingError('stage_archive_invalid')
                    files[member.name]=(member.size,_digest(data))
        except (tarfile.TarError,ValueError,OSError) as exc:
            raise StagingError('stage_archive_invalid') from exc
        encoded=b''
        if preposition is not None:
            try:
                encoded=_canonical(preposition)
                if len(encoded)>1024*1024 or set(preposition)!={'protocol','files'} or preposition['protocol']!='mycelium.controller_prepositioned_member_artifacts.v1':
                    raise ValueError()
                records=preposition['files']
                if not isinstance(records,list) or len(records)>256 or self.cache is None:
                    raise ValueError()
                destinations=[]
                for row in records:
                    if set(row)!={'content_digest','destination_path','size_bytes','source_path'}: raise ValueError()
                    relative=str(_relative(row['destination_path']))
                    source=Path(row['source_path'])
                    if not source.is_absolute() or source.resolve()!=source: raise ValueError()
                    source.relative_to(self.cache)
                    info=_regular(source)
                    size=row['size_bytes']
                    if type(size) is not int or size<0 or size!=info.st_size or relative in files: raise ValueError()
                    files[relative]=(size,_pin(row['content_digest']))
                    destinations.append(relative)
                if destinations!=sorted(set(destinations)): raise ValueError()
            except (TypeError,ValueError,OSError,KeyError):
                raise StagingError('stage_preposition_invalid') from None
        if len(archive)+sum(size for size,digest in files.values())>self.max_disk_bytes:
            raise StagingError('stage_disk_limit_exceeded')
        return files,encoded

    def _clonable(self,preposition):
        if preposition is None or sys.platform!='darwin': return set()
        try:
            device=os.stat(self.root).st_dev
            return {row['destination_path'] for row in preposition['files']
                    if os.stat(row['source_path'],follow_symlinks=False).st_dev==device}
        except (OSError,KeyError,TypeError): return set()

    def _verify(self,files):
        try:
            observed=set()
            for root,dirs,names in os.walk(self.staging_root,followlinks=False):
                if any((Path(root)/name).is_symlink() for name in dirs): raise StagingError('stage_file_unsafe')
                for name in names:
                    path=Path(root)/name
                    if path==self.staging_root/'.mycelium-stage.json': continue
                    observed.add(path.relative_to(self.staging_root).as_posix())
            if observed!=set(files): raise StagingError('stage_content_mismatch')
            for name,(size,digest) in files.items():
                path=self.staging_root/name
                if _regular(path).st_size!=size: raise StagingError('stage_content_mismatch')
                value=hashlib.sha256()
                with path.open('rb') as handle:
                    while chunk:=handle.read(1024*1024): value.update(chunk)
                if 'sha256:'+value.hexdigest()!=digest: raise StagingError('stage_content_mismatch')
        except OSError: raise StagingError('stage_content_mismatch') from None

    def _run(self,operation,args,data=b''):
        # Fixed package entrypoint, never an executable/script supplied by the job.
        if getattr(sys,'frozen',False):
            command=[sys.executable,'--managed-stage',operation,*args]
        else:
            command=[sys.executable,'-B','-m','mycelium_a13_macos.managed_staging',operation,*args]
        try:
            from .child_environment import source_mode_environment
            result=subprocess.run(command,input=data,capture_output=True,timeout=60,env=source_mode_environment())
        except subprocess.TimeoutExpired: raise StagingError('stage_operation_timeout') from None
        if result.returncode or result.stderr or len(result.stdout)>4096:
            raise StagingError('stage_operation_rejected')
        try: ack=json.loads(result.stdout)
        except (ValueError,UnicodeError): raise StagingError('stage_ack_invalid') from None
        if result.stdout!=_canonical(ack): raise StagingError('stage_ack_invalid')
        return ack

    def prepare(self,*,archive,archive_digest,preposition=None):
        if not isinstance(archive,bytes) or not 0<len(archive)<=self.max_archive_bytes:
            raise StagingError('stage_archive_limit_exceeded')
        if _digest(archive)!=_pin(archive_digest): raise StagingError('stage_archive_digest_mismatch')
        files,encoded=self._files(archive,preposition)
        marker={'node_id':self.node_id,'archive_digest':archive_digest}
        args=[str(self.staging_root),self.node_id,archive_digest,str(len(archive))]
        ack={'protocol':'mycelium.controller_remote_stage_ack.v1',**marker,
             'staging_root':str(self.staging_root),'archive_size_bytes':len(archive)}
        if preposition is not None:
            digest=_digest(encoded); marker['preposition_digest']=digest
            args.extend([digest,str(len(encoded))])
            ack.update(preposition_digest=digest,preposition_size_bytes=len(encoded))
        with self._lock():
            self._admit({'operation':'prepare',**marker,'archive_size_bytes':len(archive),
                'preposition_size_bytes':len(encoded),'staged_bytes':sum(size for size,d in files.values())})
            if os.path.lexists(self.staging_root):
                if self.staging_root.is_symlink() or _marker(self.staging_root/'.mycelium-stage.json')!=marker:
                    raise StagingError('stage_existing_binding_mismatch')
                self._verify(files)
                return ack
            # Prepositioned files on this volume become APFS clones in the canonical
            # script (no new data blocks); it re-checks free disk before any byte copy.
            clones=self._clonable(preposition)
            needed=len(archive)+len(encoded)+sum(size for name,(size,d) in files.items() if name not in clones)
            if shutil.disk_usage(self.root).free<needed: raise StagingError('stage_disk_unavailable')
            actual=self._run('prepare',args,archive+encoded)
            if actual!=ack: raise StagingError('stage_ack_invalid')
            self._verify(files)
            return actual

    def verify_prepared(self,*,archive,archive_digest,preposition=None):
        if not isinstance(archive,bytes) or not 0<len(archive)<=self.max_archive_bytes or _digest(archive)!=_pin(archive_digest):
            raise StagingError('stage_archive_digest_mismatch')
        files,encoded=self._files(archive,preposition)
        marker={'node_id':self.node_id,'archive_digest':archive_digest}
        if preposition is not None: marker['preposition_digest']=_digest(encoded)
        with self._lock():
            if self.staging_root.is_symlink() or _marker(self.staging_root/'.mycelium-stage.json')!=marker:
                raise StagingError('stage_existing_binding_mismatch')
            self._verify(files)
        return marker

    def cleanup(self,*,archive_digest,preposition_digest=None):
        marker={'node_id':self.node_id,'archive_digest':_pin(archive_digest)}
        if preposition_digest is not None: marker['preposition_digest']=_pin(preposition_digest)
        with self._lock():
            # Explicit local cleanup authorization remains available after revoke.
            try: permitted=self.authorize({'operation':'cleanup',**marker}) is True
            except Exception: permitted=False
            if not permitted: raise StagingError('stage_not_authorized')
            if os.path.lexists(self.staging_root):
                if self.staging_root.is_symlink(): raise StagingError('stage_cleanup_binding_mismatch')
                path=self.staging_root/'.mycelium-stage.json'
                if not path.exists(): path=self.staging_root/'.mycelium-stage-in-progress.json'
                if _marker(path)!=marker: raise StagingError('stage_cleanup_binding_mismatch')
            ack=self._run('cleanup',[str(self.staging_root),self.node_id,archive_digest,preposition_digest or '-'])
            if (ack.get('protocol')!='mycelium.controller_remote_cleanup_ack.v1' or
                ack.get('node_id')!=self.node_id or ack.get('staging_root')!=str(self.staging_root) or
                type(ack.get('removed')) is not bool or set(ack)!={'protocol','node_id','staging_root','removed'}):
                raise StagingError('stage_ack_invalid')
            return ack


def canonical_main():
    # Only installed source constants are evaluated. Incoming bytes are tar/data.
    from physical_inference_qualification import _REMOTE_STAGE_SCRIPT,_REMOTE_CLEANUP_SCRIPT
    if len(sys.argv)<2 or sys.argv[1] not in ('prepare','cleanup'): return 2
    script=_REMOTE_STAGE_SCRIPT if sys.argv[1]=='prepare' else _REMOTE_CLEANUP_SCRIPT
    sys.argv=[sys.argv[0],*sys.argv[2:]]
    exec(compile(script,'<installed-canonical-staging>','exec'),{'__name__':'__main__'})
    return 0


if __name__=='__main__':
    raise SystemExit(canonical_main())
