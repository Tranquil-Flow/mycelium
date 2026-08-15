"""Operator-side delivery of exact recipient artifact-acquisition jobs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import tarfile
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

from mycelium_node.identity import NodeIdentityError, load_node_signer
from mycelium_qualification.signing import build_ed25519_verifier
from mycelium_swarm_artifacts import (
    GRANT_PROTOCOL,
    SwarmArtifactContractError,
    sign_grant,
    validate_availability_bundle,
    validate_acquisition_status,
    validate_stage_pack_manifest,
)

from .local_preparer import MemberStagePackPromotion
from .preparation import ModelPreparationError


TRANSPORT_PLAN_PROTOCOL = "mycelium.member_artifact_transport_plan.v1"
RUNTIME_MANIFEST_PROTOCOL = "mycelium.member_runtime_closure_manifest.v1"
_PLAN_FIELDS = frozenset(
    {
        "protocol",
        "provisioner_generation",
        "provisioner_identity_key_file",
        "tls_ca_file",
        "predicted_improvement_ratio",
        "serving_reserve_satisfied",
        "sources",
        "recipients",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "member_id",
        "membership_generation",
        "endpoint",
        "verification_key",
        "control",
        "python_executable",
        "object_store_root",
        "manifest_inbox_directory",
        "availability_bundle_file",
    }
)
_LOCAL_CONTROL_FIELDS = frozenset({"transport"})
_SSH_CONTROL_FIELDS = frozenset(
    {"transport", "target", "port", "identity_file"}
)
_RECIPIENT_FIELDS = frozenset(
    {
        "artifact_store_root",
        "job_root",
        "recipient_identity_key_file",
        "python_executable",
        "python_path_root",
        "runtime_manifest_file",
    }
)
_GRANT_TTL_MS = 15 * 60 * 1_000
_PUBLIC_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_SSH_TARGET = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\Z")
_REMOTE_STAGE_SCRIPT = r'''import hashlib,json,os,shutil,sys,tarfile
from pathlib import Path,PurePosixPath
root=Path(sys.argv[1]);expected=sys.argv[2];size=int(sys.argv[3]);created=False
try:
    if not root.is_absolute() or str(root)!=sys.argv[1] or len(root.parts)<4 or root.exists():raise ValueError("root")
    current=Path(root.anchor)
    for part in root.parts[1:-1]:
        current=current/part
        if current.exists() and current.is_symlink():raise ValueError("symlink")
    root.mkdir(parents=True,mode=0o700,exist_ok=False);created=True
    digest=hashlib.sha256();received=0;archive=root/".incoming.tar"
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    if hasattr(os,"O_NOFOLLOW"):flags|=os.O_NOFOLLOW
    with os.fdopen(os.open(archive,flags,0o600),"wb") as output:
        while received<size:
            block=sys.stdin.buffer.read(min(1_048_576,size-received))
            if not block:raise ValueError("size")
            output.write(block);digest.update(block);received+=len(block)
        if sys.stdin.buffer.read(1):raise ValueError("size")
    actual="sha256:"+digest.hexdigest()
    if actual!=expected:raise ValueError("digest")
    with tarfile.open(archive,"r:") as source:
        members=source.getmembers();names=[member.name for member in members]
        if not members or len(members)>2048 or names!=sorted(names) or len(names)!=len(set(names)):raise ValueError("members")
        for member in members:
            relative=PurePosixPath(member.name)
            if not member.isfile() or relative.is_absolute() or str(relative)!=member.name or any(part in ("",".","..") for part in relative.parts):raise ValueError("member")
            handle=source.extractfile(member)
            if handle is None:raise ValueError("content")
            destination=root.joinpath(*relative.parts);destination.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
            with destination.open("xb") as output:shutil.copyfileobj(handle,output,1_048_576)
            destination.chmod(0o600 if member.name in {"grant.json","job.json"} else 0o400)
    archive.unlink()
    ack={"archive_digest":actual,"archive_size_bytes":received,"protocol":"mycelium.member_artifact_job_stage_ack.v1","work_root":str(root)}
    sys.stdout.write(json.dumps(ack,sort_keys=True,separators=(",",":"))+"\n")
except BaseException:
    if created:shutil.rmtree(root,ignore_errors=True)
    sys.stderr.write("member_job_stage_rejected\n");raise SystemExit(2)
'''
_REMOTE_REGISTER_MANIFEST_SCRIPT = r'''import hashlib,json,os,stat,sys
from pathlib import Path
inbox=Path(sys.argv[1]);name=sys.argv[2];expected=sys.argv[3];size=int(sys.argv[4]);temporary=None
try:
    metadata=inbox.lstat()
    if not inbox.is_absolute() or str(inbox)!=sys.argv[1] or not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid!=os.geteuid() or stat.S_IMODE(metadata.st_mode)&0o077:raise ValueError("inbox")
    if name!=expected.removeprefix("sha256:")+".json":raise ValueError("name")
    encoded=sys.stdin.buffer.read(size)
    if len(encoded)!=size or sys.stdin.buffer.read(1):raise ValueError("content")
    document=json.loads(encoded)
    canonical=lambda value:json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
    if not isinstance(document,dict) or encoded!=canonical(document)+b"\n" or document.get("manifest_digest")!=expected or "sha256:"+hashlib.sha256(canonical({key:value for key,value in document.items() if key!="manifest_digest"})).hexdigest()!=expected:raise ValueError("document")
    destination=inbox/name
    if destination.exists():
        prior=destination.lstat()
        if not stat.S_ISREG(prior.st_mode) or stat.S_ISLNK(prior.st_mode) or prior.st_uid!=os.geteuid() or prior.st_nlink!=1 or prior.st_size!=size:raise ValueError("existing")
        flags=os.O_RDONLY
        if hasattr(os,"O_NOFOLLOW"):flags|=os.O_NOFOLLOW
        with os.fdopen(os.open(destination,flags),"rb") as source:existing=source.read(size+1);final=os.fstat(source.fileno())
        if final.st_ino!=prior.st_ino or final.st_size!=prior.st_size or final.st_mtime_ns!=prior.st_mtime_ns or final.st_ctime_ns!=prior.st_ctime_ns:raise ValueError("existing")
        if existing!=encoded:raise ValueError("conflict")
        os.utime(destination,None,follow_symlinks=False)
    else:
        temporary=inbox/("."+name+"."+str(os.getpid())+".tmp")
        flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
        if hasattr(os,"O_NOFOLLOW"):flags|=os.O_NOFOLLOW
        with os.fdopen(os.open(temporary,flags,0o400),"wb") as output:output.write(encoded);output.flush();os.fsync(output.fileno())
        os.replace(temporary,destination)
        parent=os.open(inbox,os.O_RDONLY)
        try:os.fsync(parent)
        finally:os.close(parent)
    ack={"content_digest":expected,"manifest_file":str(destination),"protocol":"mycelium.member_manifest_registration_ack.v1","size_bytes":size}
    sys.stdout.write(json.dumps(ack,sort_keys=True,separators=(",",":"))+"\n")
except BaseException:
    if temporary is not None:temporary.unlink(missing_ok=True)
    sys.stderr.write("member_manifest_registration_rejected\n");raise SystemExit(2)
'''
_REMOTE_INSTALL_OBJECT_SCRIPT = r'''import hashlib,os,stat,sys
from pathlib import Path
root=Path(sys.argv[1]);expected=sys.argv[2];size=int(sys.argv[3]);temporary=None
try:
    metadata=root.lstat()
    if not root.is_absolute() or str(root)!=sys.argv[1] or not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid!=os.geteuid() or stat.S_IMODE(metadata.st_mode)&0o077:raise ValueError("root")
    if not expected.startswith("sha256:") or len(expected)!=71:raise ValueError("digest")
    name=expected.removeprefix("sha256:");destination=root/name
    if destination.exists():
        prior=destination.lstat()
        if not stat.S_ISREG(prior.st_mode) or stat.S_ISLNK(prior.st_mode) or prior.st_uid!=os.geteuid() or prior.st_nlink!=1 or prior.st_size!=size:raise ValueError("existing")
        digest=hashlib.sha256()
        flags=os.O_RDONLY
        if hasattr(os,"O_NOFOLLOW"):flags|=os.O_NOFOLLOW
        with os.fdopen(os.open(destination,flags),"rb") as source:
            while block:=source.read(1_048_576):digest.update(block)
        if "sha256:"+digest.hexdigest()!=expected:raise ValueError("existing")
        repeated=sys.stdin.buffer.read(size+1)
        if repeated and (len(repeated)!=size or "sha256:"+hashlib.sha256(repeated).hexdigest()!=expected):raise ValueError("unexpected")
    else:
        temporary=root/("."+name+"."+str(os.getpid())+".tmp")
        flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
        if hasattr(os,"O_NOFOLLOW"):flags|=os.O_NOFOLLOW
        digest=hashlib.sha256();received=0
        with os.fdopen(os.open(temporary,flags,0o400),"wb") as output:
            while received<size:
                block=sys.stdin.buffer.read(min(1_048_576,size-received))
                if not block:raise ValueError("size")
                output.write(block);digest.update(block);received+=len(block)
            if sys.stdin.buffer.read(1) or "sha256:"+digest.hexdigest()!=expected:raise ValueError("content")
            output.flush();os.fsync(output.fileno())
        os.replace(temporary,destination);temporary=None
        parent=os.open(root,os.O_RDONLY)
        try:os.fsync(parent)
        finally:os.close(parent)
    sys.stdout.write(expected+"\n")
except BaseException:
    if temporary is not None:temporary.unlink(missing_ok=True)
    sys.stderr.write("member_source_object_install_rejected\n");raise SystemExit(2)
'''
_REMOTE_READ_BUNDLE_SCRIPT = r'''import os,stat,sys
from pathlib import Path
path=Path(sys.argv[1]);maximum=int(sys.argv[2])
try:
    metadata=path.lstat()
    if not path.is_absolute() or str(path)!=sys.argv[1] or not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid!=os.geteuid() or metadata.st_nlink!=1 or not 0<metadata.st_size<=maximum:raise ValueError("file")
    flags=os.O_RDONLY
    if hasattr(os,"O_NOFOLLOW"):flags|=os.O_NOFOLLOW
    with os.fdopen(os.open(path,flags),"rb") as source:encoded=source.read(maximum+1)
    if len(encoded)!=metadata.st_size or len(encoded)>maximum:raise ValueError("size")
    sys.stdout.buffer.write(encoded)
except BaseException:
    sys.stderr.write("member_availability_read_rejected\n");raise SystemExit(2)
'''
_REMOTE_EXECUTE_SCRIPT = r'''import hashlib,json,os,runpy,stat,sys
from pathlib import Path,PurePosixPath
runtime=Path(sys.argv[1]);manifest_path=Path(sys.argv[2]);job=Path(sys.argv[3])
try:
    manifest=json.loads(manifest_path.read_text("utf-8"))
    if set(manifest)!={"files","protocol"} or manifest["protocol"]!="mycelium.member_runtime_closure_manifest.v1" or not isinstance(manifest["files"],list):raise ValueError("manifest")
    paths=[]
    for record in manifest["files"]:
        if not isinstance(record,dict) or set(record)!={"content_digest","path","size_bytes"}:raise ValueError("record")
        relative=PurePosixPath(record["path"]);candidate=runtime.joinpath(*relative.parts)
        if relative.is_absolute() or str(relative)!=record["path"] or any(part in ("",".","..") for part in relative.parts):raise ValueError("path")
        metadata=candidate.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid!=os.geteuid() or metadata.st_size!=record["size_bytes"]:raise ValueError("file")
        digest=hashlib.sha256()
        with candidate.open("rb") as source:
            while block:=source.read(1_048_576):digest.update(block)
        if "sha256:"+digest.hexdigest()!=record["content_digest"]:raise ValueError("digest")
        paths.append(record["path"])
    if paths!=sorted(paths) or len(paths)!=len(set(paths)):raise ValueError("order")
    sys.path.insert(0,str(runtime));sys.argv=["member_artifact_provisioner","--job",str(job)]
    runpy.run_module("mycelium_live.member_artifact_provisioner",run_name="__main__")
except SystemExit:raise
except BaseException:
    sys.stderr.write("member_job_execute_rejected\n");raise SystemExit(2)
'''


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _member_execution_status(
    executed: subprocess.CompletedProcess[bytes],
) -> dict[str, Any]:
    try:
        result = json.loads(executed.stdout)
    except (UnicodeError, json.JSONDecodeError):
        result = None
    if executed.returncode != 0:
        reason = result.get("reason_code") if isinstance(result, dict) else None
        if (
            executed.stderr == b""
            and isinstance(result, dict)
            and set(result) == {"protocol", "reason_code"}
            and result.get("protocol")
            == "mycelium.member_artifact_acquisition_failure.v1"
            and isinstance(reason, str)
            and _PUBLIC_REASON.fullmatch(reason) is not None
            and executed.stdout == _canonical(result)
        ):
            raise ModelPreparationError(reason)
        raise ModelPreparationError("member_artifact_job_execution_failed")
    if executed.stderr:
        raise ModelPreparationError("member_artifact_job_execution_failed")
    try:
        if (
            not isinstance(result, dict)
            or set(result) != {"protocol", "status"}
            or result.get("protocol")
            != "mycelium.member_artifact_acquisition_result.v1"
            or executed.stdout != _canonical(result)
        ):
            raise ValueError("result")
        return validate_acquisition_status(result["status"])
    except (KeyError, TypeError, ValueError, SwarmArtifactContractError) as exc:
        raise ModelPreparationError("member_artifact_job_result_invalid") from exc


def _absolute_file(value: object, code: str, *, private: bool = False) -> Path:
    if not isinstance(value, str):
        raise ModelPreparationError(code)
    path = Path(value)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ModelPreparationError(code) from exc
    if (
        not path.is_absolute()
        or resolved != path
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or metadata.st_size > 16 * 1024 * 1024
        or (private and stat.S_IMODE(metadata.st_mode) != 0o600)
    ):
        raise ModelPreparationError(code)
    return path


def _remote_path(value: object, code: str) -> str:
    path = PurePosixPath(value) if isinstance(value, str) else None
    if (
        path is None
        or not path.is_absolute()
        or str(path) != value
        or len(value) > 2048
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ModelPreparationError(code)
    return value


def _document(path: Path, code: str, *, private: bool = False) -> dict[str, Any]:
    source = _absolute_file(str(path), code, private=private)
    try:
        encoded = source.read_bytes()
        value = json.loads(encoded)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelPreparationError(code) from exc
    if not isinstance(value, dict) or encoded != _canonical(value):
        raise ModelPreparationError(code)
    return value


class MemberArtifactTransport:
    """Callable remote member executor loaded from an owner-private plan."""

    def __init__(
        self,
        plan_file: Path,
        *,
        run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        clock_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        plan = _document(
            Path(plan_file), "member_artifact_transport_plan_invalid", private=True
        )
        if set(plan) != _PLAN_FIELDS or plan.get("protocol") != TRANSPORT_PLAN_PROTOCOL:
            raise ModelPreparationError("member_artifact_transport_plan_invalid")
        if (
            type(plan.get("provisioner_generation")) is not int
            or plan["provisioner_generation"] < 1
            or type(plan.get("predicted_improvement_ratio")) not in {int, float}
            or not 0 <= float(plan["predicted_improvement_ratio"]) <= 1
            or type(plan.get("serving_reserve_satisfied")) is not bool
            or not isinstance(plan.get("sources"), list)
            or not plan["sources"]
            or not isinstance(plan.get("recipients"), Mapping)
            or not plan["recipients"]
        ):
            raise ModelPreparationError("member_artifact_transport_plan_invalid")
        _absolute_file(
            plan["provisioner_identity_key_file"],
            "member_artifact_provisioner_identity_invalid",
            private=True,
        )
        _absolute_file(plan["tls_ca_file"], "member_artifact_tls_ca_invalid")
        source_ids: set[str] = set()
        for source in plan["sources"]:
            try:
                endpoint = urlsplit(source.get("endpoint"))
                endpoint_port = endpoint.port
            except (TypeError, ValueError) as exc:
                raise ModelPreparationError(
                    "member_artifact_source_plan_invalid"
                ) from exc
            control = source.get("control") if isinstance(source, Mapping) else None
            if (
                not isinstance(source, Mapping)
                or set(source) != _SOURCE_FIELDS
                or not isinstance(source.get("member_id"), str)
                or not source["member_id"]
                or source["member_id"] in source_ids
                or type(source.get("membership_generation")) is not int
                or source["membership_generation"] < 1
                or not isinstance(source.get("endpoint"), str)
                or endpoint.scheme != "https"
                or endpoint.hostname is None
                or endpoint_port is None
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.path not in {"", "/"}
                or endpoint.query
                or endpoint.fragment
                or not isinstance(source.get("verification_key"), Mapping)
                or not isinstance(control, Mapping)
            ):
                raise ModelPreparationError("member_artifact_source_plan_invalid")
            transport = control.get("transport")
            if transport == "local":
                if set(control) != _LOCAL_CONTROL_FIELDS:
                    raise ModelPreparationError(
                        "member_artifact_source_control_invalid"
                    )
            elif transport == "ssh":
                target = control.get("target")
                port = control.get("port")
                if (
                    set(control) != _SSH_CONTROL_FIELDS
                    or not isinstance(target, str)
                    or _SSH_TARGET.fullmatch(target) is None
                    or type(port) is not int
                    or not 1 <= port <= 65_535
                ):
                    raise ModelPreparationError(
                        "member_artifact_source_control_invalid"
                    )
                _absolute_file(
                    control.get("identity_file"),
                    "member_artifact_source_control_invalid",
                    private=True,
                )
            else:
                raise ModelPreparationError("member_artifact_source_control_invalid")
            _remote_path(
                source.get("python_executable"),
                "member_artifact_source_plan_invalid",
            )
            _remote_path(
                source.get("object_store_root"),
                "member_artifact_source_plan_invalid",
            )
            _remote_path(
                source.get("manifest_inbox_directory"),
                "member_artifact_source_plan_invalid",
            )
            _remote_path(
                source.get("availability_bundle_file"),
                "member_artifact_source_plan_invalid",
            )
            source_ids.add(source["member_id"])
        for member_id, recipient in plan["recipients"].items():
            if (
                not isinstance(member_id, str)
                or not member_id
                or not isinstance(recipient, Mapping)
                or set(recipient) != _RECIPIENT_FIELDS
            ):
                raise ModelPreparationError("member_artifact_recipient_plan_invalid")
            for field in (
                "artifact_store_root",
                "job_root",
                "recipient_identity_key_file",
                "python_executable",
                "python_path_root",
            ):
                _remote_path(
                    recipient.get(field), "member_artifact_recipient_plan_invalid"
                )
            runtime = _document(
                Path(recipient["runtime_manifest_file"]),
                "member_artifact_runtime_manifest_invalid",
            )
            if (
                set(runtime) != {"protocol", "files"}
                or runtime.get("protocol") != RUNTIME_MANIFEST_PROTOCOL
                or not isinstance(runtime.get("files"), list)
                or not runtime["files"]
            ):
                raise ModelPreparationError("member_artifact_runtime_manifest_invalid")
            runtime_paths = [record.get("path") for record in runtime["files"]]
            if runtime_paths != sorted(runtime_paths) or len(runtime_paths) != len(
                set(runtime_paths)
            ):
                raise ModelPreparationError("member_artifact_runtime_manifest_invalid")
            for record in runtime["files"]:
                relative = (
                    PurePosixPath(record.get("path"))
                    if isinstance(record, Mapping)
                    and isinstance(record.get("path"), str)
                    else None
                )
                if (
                    not isinstance(record, Mapping)
                    or set(record) != {"path", "size_bytes", "content_digest"}
                    or relative is None
                    or relative.is_absolute()
                    or str(relative) != record["path"]
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or type(record.get("size_bytes")) is not int
                    or record["size_bytes"] < 0
                    or not isinstance(record.get("content_digest"), str)
                    or len(record["content_digest"]) != 71
                    or not record["content_digest"].startswith("sha256:")
                    or any(
                        character not in "0123456789abcdef"
                        for character in record["content_digest"][7:]
                    )
                ):
                    raise ModelPreparationError(
                        "member_artifact_runtime_manifest_invalid"
                    )
        self._plan = plan
        try:
            self._signer = load_node_signer(
                Path(plan["provisioner_identity_key_file"]),
                endpoint_id="artifact-provisioner",
            )
        except NodeIdentityError as exc:
            raise ModelPreparationError(
                "member_artifact_provisioner_identity_invalid"
            ) from exc
        self._run = run
        self._clock = clock_unix_ms or (lambda: int(time.time() * 1_000))

    @staticmethod
    def _ssh(peer: Mapping[str, Any], remote: tuple[str, ...]) -> list[str]:
        target = peer.get("ssh_target")
        identity = peer.get("ssh_identity_file")
        if not isinstance(target, str) or not isinstance(identity, str):
            raise ModelPreparationError("member_artifact_peer_transport_invalid")
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            "-i",
            identity,
            "--",
            target,
            shlex.join(remote),
        ]

    @staticmethod
    def _source_argv(
        source: Mapping[str, Any], remote: tuple[str, ...]
    ) -> list[str]:
        control = source["control"]
        if control["transport"] == "local":
            return list(remote)
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            "-p",
            str(control["port"]),
            "-i",
            control["identity_file"],
            "--",
            control["target"],
            shlex.join(remote),
        ]

    @staticmethod
    def _archive(files: Mapping[str, bytes]) -> bytes:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name in sorted(files):
                payload = files[name]
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                member.mode = 0o600
                member.uid = member.gid = member.mtime = 0
                tar.addfile(member, io.BytesIO(payload))
        return stream.getvalue()

    @staticmethod
    def _local_inbox(value: str) -> Path:
        inbox = Path(value)
        try:
            metadata = inbox.lstat()
            resolved = inbox.resolve(strict=True)
        except OSError as exc:
            raise ModelPreparationError(
                "member_artifact_manifest_registration_failed"
            ) from exc
        if (
            resolved != inbox
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ModelPreparationError(
                "member_artifact_manifest_registration_failed"
            )
        return inbox

    def _register_source_manifest(
        self,
        *,
        source: Mapping[str, Any],
        manifest_bytes: bytes,
        manifest_digest: str,
    ) -> None:
        name = manifest_digest.removeprefix("sha256:") + ".json"
        if source["control"]["transport"] == "local":
            inbox = self._local_inbox(source["manifest_inbox_directory"])
            destination = inbox / name
            if destination.exists():
                try:
                    if _absolute_file(
                        str(destination),
                        "member_artifact_manifest_registration_failed",
                    ).read_bytes() != manifest_bytes:
                        raise ModelPreparationError(
                            "member_artifact_manifest_registration_conflict"
                        )
                except OSError as exc:
                    raise ModelPreparationError(
                        "member_artifact_manifest_registration_failed"
                    ) from exc
                try:
                    os.utime(destination, None, follow_symlinks=False)
                except OSError as exc:
                    raise ModelPreparationError(
                        "member_artifact_manifest_registration_failed"
                    ) from exc
                return
            temporary = inbox / f".{name}.{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o400,
                )
                with os.fdopen(descriptor, "wb") as output:
                    output.write(manifest_bytes)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
                parent = os.open(inbox, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            except OSError as exc:
                raise ModelPreparationError(
                    "member_artifact_manifest_registration_failed"
                ) from exc
            finally:
                temporary.unlink(missing_ok=True)
            return
        capture = self._run(
            self._source_argv(
                source,
                (
                    source["python_executable"],
                    "-c",
                    _REMOTE_REGISTER_MANIFEST_SCRIPT,
                    source["manifest_inbox_directory"],
                    name,
                    manifest_digest,
                    str(len(manifest_bytes)),
                ),
            ),
            input=manifest_bytes,
            capture_output=True,
            timeout=60,
            check=False,
        )
        expected = {
            "protocol": "mycelium.member_manifest_registration_ack.v1",
            "manifest_file": f"{source['manifest_inbox_directory']}/{name}",
            "content_digest": manifest_digest,
            "size_bytes": len(manifest_bytes),
        }
        if (
            capture.returncode != 0
            or capture.stderr
            or capture.stdout != _canonical(expected)
        ):
            raise ModelPreparationError(
                "member_artifact_manifest_registration_failed"
            )

    def _install_source_object(
        self,
        *,
        source: Mapping[str, Any],
        object_path: Path,
        digest: str,
        size_bytes: int,
    ) -> None:
        try:
            payload = object_path.read_bytes()
        except OSError as exc:
            raise ModelPreparationError("member_artifact_source_object_missing") from exc
        if (
            len(payload) != size_bytes
            or "sha256:" + hashlib.sha256(payload).hexdigest() != digest
        ):
            raise ModelPreparationError("member_artifact_source_object_invalid")
        if source["control"]["transport"] == "local":
            root = self._local_inbox(source["object_store_root"])
            destination = root / digest.removeprefix("sha256:")
            if destination.exists():
                try:
                    if _absolute_file(
                        str(destination), "member_artifact_source_object_invalid"
                    ).read_bytes() != payload:
                        raise ModelPreparationError(
                            "member_artifact_source_object_conflict"
                        )
                except OSError as exc:
                    raise ModelPreparationError(
                        "member_artifact_source_object_invalid"
                    ) from exc
                return
            temporary = root / f".{destination.name}.{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o400,
                )
                with os.fdopen(descriptor, "wb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
                parent = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            except OSError as exc:
                raise ModelPreparationError(
                    "member_artifact_source_object_install_failed"
                ) from exc
            finally:
                temporary.unlink(missing_ok=True)
            return
        installed = self._run(
            self._source_argv(
                source,
                (
                    source["python_executable"],
                    "-c",
                    _REMOTE_INSTALL_OBJECT_SCRIPT,
                    source["object_store_root"],
                    digest,
                    str(size_bytes),
                ),
            ),
            input=payload,
            capture_output=True,
            timeout=300,
            check=False,
        )
        if (
            installed.returncode != 0
            or installed.stderr
            or installed.stdout != (digest + "\n").encode()
        ):
            raise ModelPreparationError(
                "member_artifact_source_object_install_failed"
            )

    def _seed_source_objects(
        self,
        *,
        manifest: Mapping[str, Any],
        stage_source: Path,
    ) -> None:
        sources = self._plan["sources"]
        chunks = manifest["chunks"]
        if len(sources) > 1 and len(chunks) < 2:
            raise ModelPreparationError("member_artifact_multi_source_not_useful")
        for chunk in chunks:
            for source in sources:
                self._install_source_object(
                    source=source,
                    object_path=(
                        stage_source
                        / "objects"
                        / chunk["content_digest"].removeprefix("sha256:")
                    ),
                    digest=chunk["content_digest"],
                    size_bytes=chunk["size_bytes"],
                )

    def _read_source_bundle(
        self,
        *,
        source: Mapping[str, Any],
    ) -> bytes:
        if source["control"]["transport"] == "local":
            return _absolute_file(
                source["availability_bundle_file"],
                "member_artifact_availability_file_invalid",
            ).read_bytes()
        capture = self._run(
            self._source_argv(
                source,
                (
                    source["python_executable"],
                    "-c",
                    _REMOTE_READ_BUNDLE_SCRIPT,
                    source["availability_bundle_file"],
                    str(16 * 1024 * 1024),
                ),
            ),
            capture_output=True,
            timeout=30,
            check=False,
        )
        if capture.returncode != 0 or capture.stderr or not capture.stdout:
            raise ModelPreparationError(
                "member_artifact_availability_not_ready"
            )
        return capture.stdout

    def _reconcile_source_bundles(
        self,
        *,
        manifest: Mapping[str, Any],
    ) -> dict[str, bytes]:
        encoded_manifest = _canonical(manifest)
        for source in self._plan["sources"]:
            self._register_source_manifest(
                source=source,
                manifest_bytes=encoded_manifest,
                manifest_digest=manifest["manifest_digest"],
            )
        pending = {source["member_id"] for source in self._plan["sources"]}
        bundles: dict[str, bytes] = {}
        deadline = time.monotonic() + 30.0
        while pending and time.monotonic() < deadline:
            for source in self._plan["sources"]:
                source_id = source["member_id"]
                if source_id not in pending:
                    continue
                try:
                    encoded = self._read_source_bundle(source=source)
                    raw = json.loads(encoded)
                    checked = validate_availability_bundle(
                        raw,
                        verifier=build_ed25519_verifier(
                            [source["verification_key"]]
                        ),
                        now_unix_ms=self._clock(),
                        expected_source_member_id=source_id,
                        expected_membership_generation=source[
                            "membership_generation"
                        ],
                    )
                    matching = [
                        item
                        for item in checked["advertisements"]
                        if item["manifest_digest"] == manifest["manifest_digest"]
                    ]
                    if len(matching) != 1:
                        continue
                except (
                    ModelPreparationError,
                    OSError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                    SwarmArtifactContractError,
                ):
                    continue
                bundles[source_id] = _canonical(checked)
                pending.remove(source_id)
            if pending:
                time.sleep(0.1)
        if pending:
            raise ModelPreparationError("member_artifact_availability_not_ready")
        return bundles

    def __call__(
        self,
        *,
        manifest: Mapping[str, Any],
        expected_binding: Mapping[str, Any],
        policy: Mapping[str, Any],
        stage_source: Path,
        peer: Mapping[str, Any],
    ) -> MemberStagePackPromotion:
        try:
            frozen_manifest = validate_stage_pack_manifest(
                manifest, expected_binding=expected_binding
            )
        except SwarmArtifactContractError as exc:
            raise ModelPreparationError(exc.code) from exc
        member_id = frozen_manifest["recipient_member_id"]
        recipient = self._plan["recipients"].get(member_id)
        if not isinstance(recipient, Mapping):
            raise ModelPreparationError("member_artifact_recipient_unconfigured")
        now = self._clock()
        expires = min(frozen_manifest["expires_at_unix_ms"], now + _GRANT_TTL_MS)
        if expires <= now:
            raise ModelPreparationError("member_artifact_authority_stale")
        source_ids = sorted(source["member_id"] for source in self._plan["sources"])
        self._seed_source_objects(
            manifest=frozen_manifest,
            stage_source=stage_source,
        )
        source_bundles = self._reconcile_source_bundles(
            manifest=frozen_manifest,
        )
        try:
            grant = sign_grant(
                {
                    "protocol": GRANT_PROTOCOL,
                    "grant_id": "grant-" + uuid.uuid4().hex,
                    "nonce": uuid.uuid4().hex,
                    "provisioner_generation": self._plan["provisioner_generation"],
                    "recipient_member_id": member_id,
                    "recipient_membership_generation": frozen_manifest[
                        "recipient_membership_generation"
                    ],
                    "manifest_digest": frozen_manifest["manifest_digest"],
                    "assignment_digest": frozen_manifest["assignment_digest"],
                    "representation_digest": frozen_manifest["representation_digest"],
                    "feasibility_digest": frozen_manifest["feasibility_digest"],
                    "allowed_chunk_digests": sorted(
                        chunk["content_digest"] for chunk in frozen_manifest["chunks"]
                    ),
                    "maximum_total_bytes": frozen_manifest["total_size_bytes"],
                    "maximum_concurrency": policy["aggregate_concurrency"],
                    "maximum_bytes_per_second": policy[
                        "aggregate_bytes_per_second"
                    ],
                    "authorized_source_member_ids": source_ids,
                    "origin_fallback_allowed": False,
                    "issued_at_unix_ms": now,
                    "not_before_unix_ms": now,
                    "expires_at_unix_ms": expires,
                },
                self._signer,
            )
        except (SwarmArtifactContractError, KeyError) as exc:
            raise ModelPreparationError("member_artifact_grant_invalid") from exc
        work_root = str(
            PurePosixPath(recipient["job_root"])
            / (frozen_manifest["manifest_id"] + "-" + uuid.uuid4().hex[:12])
        )
        files: dict[str, bytes] = {
            "manifest.json": _canonical(frozen_manifest),
            "binding.json": _canonical(dict(expected_binding)),
            "grant.json": _canonical(grant),
            "tls-ca.pem": _absolute_file(
                self._plan["tls_ca_file"], "member_artifact_tls_ca_invalid"
            ).read_bytes(),
        }
        sources = []
        for index, source in enumerate(self._plan["sources"]):
            name = f"availability-{index:04d}.json"
            files[name] = source_bundles[source["member_id"]]
            sources.append(
                {
                    "member_id": source["member_id"],
                    "membership_generation": source["membership_generation"],
                    "endpoint": source["endpoint"],
                    "verification_key": source["verification_key"],
                    "availability_bundle_file": f"{work_root}/{name}",
                }
            )
        runtime_name = "runtime-manifest.json"
        files[runtime_name] = _absolute_file(
            recipient["runtime_manifest_file"],
            "member_artifact_runtime_manifest_invalid",
        ).read_bytes()
        status_path = f"{work_root}/status.json"
        job = {
            "protocol": "mycelium.member_artifact_acquisition_job.v1",
            "recipient_member_id": member_id,
            "recipient_membership_generation": frozen_manifest[
                "recipient_membership_generation"
            ],
            "recipient_identity_key_file": recipient[
                "recipient_identity_key_file"
            ],
            "provisioner_generation": self._plan["provisioner_generation"],
            "provisioner_verification_keys": [self._signer.public_key_record()],
            "manifest_file": f"{work_root}/manifest.json",
            "expected_binding_file": f"{work_root}/binding.json",
            "grant_file": f"{work_root}/grant.json",
            "sources": sources,
            "tls_ca_file": f"{work_root}/tls-ca.pem",
            "artifact_store_root": recipient["artifact_store_root"],
            "policy": dict(policy),
            "predicted_improvement_ratio": float(
                self._plan["predicted_improvement_ratio"]
            ),
            "serving_reserve_satisfied": self._plan[
                "serving_reserve_satisfied"
            ],
            "status_output_file": status_path,
        }
        files["job.json"] = _canonical(job)
        archive = self._archive(files)
        archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
        staged = self._run(
            self._ssh(
                peer,
                (
                    recipient["python_executable"],
                    "-c",
                    _REMOTE_STAGE_SCRIPT,
                    work_root,
                    archive_digest,
                    str(len(archive)),
                ),
            ),
            input=archive,
            capture_output=True,
            timeout=300,
            check=False,
        )
        if staged.returncode != 0 or staged.stderr:
            raise ModelPreparationError("member_artifact_job_staging_failed")
        expected_ack = {
            "protocol": "mycelium.member_artifact_job_stage_ack.v1",
            "work_root": work_root,
            "archive_digest": archive_digest,
            "archive_size_bytes": len(archive),
        }
        if staged.stdout != _canonical(expected_ack):
            raise ModelPreparationError("member_artifact_job_stage_ack_invalid")
        executed = self._run(
            self._ssh(
                peer,
                (
                    recipient["python_executable"],
                    "-c",
                    _REMOTE_EXECUTE_SCRIPT,
                    recipient["python_path_root"],
                    f"{work_root}/{runtime_name}",
                    f"{work_root}/job.json",
                ),
            ),
            capture_output=True,
            timeout=900,
            check=False,
        )
        status = _member_execution_status(executed)
        return MemberStagePackPromotion(
            member_id=member_id,
            files_root=(
                f"{recipient['artifact_store_root']}/promoted/"
                f"{frozen_manifest['manifest_id']}/files"
            ),
            status=status,
        )


__all__ = [
    "MemberArtifactTransport",
    "RUNTIME_MANIFEST_PROTOCOL",
    "TRANSPORT_PLAN_PROTOCOL",
]
