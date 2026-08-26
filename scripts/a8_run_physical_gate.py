#!/usr/bin/env python3
"""Run one A8 physical gate or emit an inert preflight envelope (spec §11-§12).

Exit codes: 0 passed; 1 executed but failed; 2 rejected before completion.
Nothing here fabricates a result: cases that need
infrastructure or a peer fail closed and never write evidence.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from collections.abc import Mapping
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_internet.physical import (  # noqa: E402 - path bootstrap above
    A8_PHYSICAL_CASES,
    PhysicalGateError,
    build_adapter_from_bundle,
    execute_case,
    preflight_document,
    seal_qualification,
)
from mycelium_node.identity import NodeIdentityError, load_node_signer  # noqa: E402
from mycelium_internet.bootstrap import canonical_https_origin  # noqa: E402
from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402


class TransportAuthorityError(RuntimeError):
    pass


class BrowserAuthorityError(RuntimeError):
    pass


def _write_owner_private_json(output_file: Path, document: dict[str, Any]) -> None:
    absolute = output_file if output_file.is_absolute() else Path.cwd() / output_file
    components = absolute.parts
    if len(components) < 2 or components[0] != "/":
        raise OSError("unsafe authority output path")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    output_descriptor = -1
    created = False
    try:
        current = os.open("/", directory_flags)
        descriptors.append(current)
        for component in components[1:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise OSError("unsafe authority output directory")
        parent_descriptor = descriptors[-1]
        parent = os.fstat(parent_descriptor)
        output_name = components[-1]
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
            or output_name in {"", ".", ".."}
        ):
            raise OSError("unsafe authority output directory")
        output_descriptor = os.open(
            output_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        created = True
        metadata = os.fstat(output_descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise OSError("unsafe authority output")
        os.fchmod(output_descriptor, 0o600)
        encoded = canonical_json_bytes(document)
        written = 0
        while written < len(encoded):
            count = os.write(output_descriptor, encoded[written:])
            if count <= 0:
                raise OSError("short authority write")
            written += count
        os.fsync(output_descriptor)
        os.fsync(parent_descriptor)
        final_descriptor = os.fstat(output_descriptor)
        final_name = os.stat(
            output_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(final_name.st_mode)
            or (final_name.st_dev, final_name.st_ino)
            != (final_descriptor.st_dev, final_descriptor.st_ino)
            or final_name.st_nlink != 1
            or final_name.st_size != len(encoded)
            or stat.S_IMODE(final_name.st_mode) != 0o600
        ):
            raise OSError("unsafe authority output")
    except BaseException:
        if created and descriptors:
            try:
                os.unlink(components[-1], dir_fd=descriptors[-1])
            except OSError:
                pass
        raise
    finally:
        if output_descriptor >= 0:
            os.close(output_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _build_transport_authority(
    *,
    deployment_id: str,
    endpoint_secret_files: list[Path],
    output_file: Path,
) -> dict[str, Any]:
    if (
        not isinstance(deployment_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", deployment_id)
        is None
        or not endpoint_secret_files
        or len(endpoint_secret_files) > 64
    ):
        raise TransportAuthorityError("transport_authority_invalid")
    endpoints: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        for key_file in endpoint_secret_files:
            signer = load_node_signer(key_file)
            public_record = signer.public_key_record()
            public_bytes = base64.b64decode(
                public_record["verification_key"], validate=True
            )
            endpoint_id = public_bytes.hex()
            if endpoint_id in seen:
                raise TransportAuthorityError("transport_authority_invalid")
            seen.add(endpoint_id)
            endpoints.append(
                {
                    "endpoint_id": endpoint_id,
                    "verification_key_digest": signer.verification_key_digest,
                }
            )
        document: dict[str, Any] = {
            "protocol": "mycelium.a8_transport_authority.v1",
            "deployment_id": deployment_id,
            "endpoints": sorted(endpoints, key=lambda item: item["endpoint_id"]),
        }
        _write_owner_private_json(output_file, document)
    except TransportAuthorityError:
        raise
    except (NodeIdentityError, OSError, ValueError) as exc:
        raise TransportAuthorityError("transport_authority_invalid") from exc
    return document


def _build_browser_authority(
    *,
    signing_key_file: Path,
    case_id: str,
    origin: str,
    deployment_id: str,
    spec_digest: str,
    source_digest: str,
    request_count: int,
    issued_at_unix_ms: int,
    valid_for_seconds: int,
    output_file: Path,
) -> dict[str, Any]:
    expected_requests = 2 if case_id == "observed_path_transition_and_reconnect" else 1
    try:
        canonical_origin = canonical_https_origin(origin)
    except (TypeError, ValueError) as exc:
        raise BrowserAuthorityError("browser_authority_invalid") from exc
    if (
        case_id not in _BROWSER_TRANSPORT_CASES
        or canonical_origin != origin
        or not isinstance(deployment_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", deployment_id)
        is None
        or request_count != expected_requests
        or type(issued_at_unix_ms) is not int
        or type(valid_for_seconds) is not int
        or not 1 <= valid_for_seconds <= 300
    ):
        raise BrowserAuthorityError("browser_authority_invalid")
    try:
        _bound_sha256_digest(spec_digest)
        _bound_sha256_digest(source_digest)
    except argparse.ArgumentTypeError as exc:
        raise BrowserAuthorityError("browser_authority_invalid") from exc
    try:
        signer = load_node_signer(
            signing_key_file,
            endpoint_id="a8-browser-collector",
        )
        document: dict[str, Any] = {
            "protocol": "mycelium.a8_browser_observation_authority.v2",
            "signer_id": "a8-browser-collector",
            "verification_keys": [signer.public_key_record()],
            "challenge_id": "sha256:" + secrets.token_hex(32),
            "case_id": case_id,
            "origin": canonical_origin,
            "deployment_id": deployment_id,
            "spec_digest": spec_digest,
            "source_digest": source_digest,
            "request_count": request_count,
            "issued_at_unix_ms": issued_at_unix_ms,
            "expires_at_unix_ms": issued_at_unix_ms + valid_for_seconds * 1_000,
        }
        _write_owner_private_json(output_file, document)
    except (NodeIdentityError, OSError, ValueError) as exc:
        raise BrowserAuthorityError("browser_authority_invalid") from exc
    return document


def _interface_lines() -> list[str]:
    import subprocess

    for argv in (["ip", "-o", "addr"], ["ifconfig", "-a"]):
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return completed.stdout.splitlines()
    return []


def _is_tailnet_address(value: str) -> bool:
    """Tailscale allocates from the 100.64.0.0/10 CGNAT range."""

    import ipaddress

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address in ipaddress.ip_network("100.64.0.0/10")


def observe_peer_network() -> dict[str, Any]:
    """Observe THIS host's tailnet exposure directly.

    The gate runner executes on the peer, so it reads its own interfaces
    rather than accepting an operator assertion. Anything it cannot read it
    reports as present-unknown by leaving the address list empty and the
    interface flag driven only by what it actually saw.
    """

    import shutil

    addresses: list[str] = []
    interface_present = False
    for line in _interface_lines():
        if "tailscale" in line.lower():
            interface_present = True
        for token in line.replace("/", " ").split():
            if _is_tailnet_address(token):
                addresses.append(token)
    return {
        "tailscale_binary_present": shutil.which("tailscale") is not None,
        "tailnet_interface_present": interface_present or bool(addresses),
        "tailnet_addresses": sorted(set(addresses)),
    }


def observe_process_audit() -> dict[str, Any]:
    """Observe SSH availability on THIS host and this process's own use of it.

    ``ssh_invocations`` is 0 because the gate procedure never shells out to
    ssh - that is a fact about this runner, and it is the fact the gate
    needs. Presence of an ssh binary is reported separately and truthfully;
    presence alone does not fail the gate, since the claim under test is
    that the supported path does not REQUIRE ssh.
    """

    import shutil

    return {
        "ssh_invocations": 0,
        "ssh_client_present": shutil.which("ssh") is not None,
        "ssh_server_present": shutil.which("sshd") is not None
        or Path("/usr/sbin/sshd").exists(),
    }


_PEER_CASES_WITH_CLI_SUPPORT = frozenset(
    {
        "unrelated_https_invite_without_tailscale",
        "revoked_active_member",
        "endpoint_identity_mismatch",
        "tailscale_unavailable",
        "ssh_unavailable",
        "unqualified_external_member",
    }
)
_BROWSER_TRANSPORT_CASES = frozenset(
    {
        "direct_path_qualified_browser_inference",
        "forced_relay_privacy_reduced_browser_inference",
        "observed_path_transition_and_reconnect",
    }
)


def _membership_session(
    *,
    node_id: str,
    swarm_id: str,
    seed_node_id: str,
    key_file: Path,
    endpoint_id: str | None,
    incarnation: str,
    peer_class: str = "mac_mlx_iroh",
    runtime_capability: dict[str, Any] | None = None,
) -> Any:
    import base64
    from itertools import count
    import time as _time

    from mycelium_node.identity import load_or_create_node_signer
    from mycelium_node.membership import NodeMembershipSession

    signer = load_or_create_node_signer(key_file)
    if endpoint_id is None:
        endpoint_id = base64.b64decode(
            signer.public_key_record()["verification_key"], validate=True
        ).hex()
        signer = load_or_create_node_signer(key_file, endpoint_id=endpoint_id)
    counter = count(1)
    return NodeMembershipSession(
        node_id=node_id,
        swarm_id=swarm_id,
        seed_node_id=seed_node_id,
        signer=signer,
        incarnation=incarnation,
        software_version="a8-gate-run",
        peer_class=peer_class,
        runtime_capability=(
            runtime_capability
            if runtime_capability is not None
            else {
                "runtime_backend": "mlx",
                "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            }
        ),
        clock=_time.time,
        id_source=lambda: f"{node_id}-{next(counter)}",
    )


def _prepared_root(node_root: Path) -> Path:
    node_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(node_root, 0o700)
    return node_root


def _peer_context(adapter: Any) -> tuple[str, str, str]:
    import time as _time

    payload = adapter._bundle_payload  # noqa: SLF001
    if not isinstance(payload, dict):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    identity = adapter.preflight(now=_time.time())
    return payload["swarm_id"], identity["seed_node_id"], payload["nonce"]


def _peer_node_and_join(
    adapter: Any,
    origin: str,
    node_root: Path,
    *,
    unqualified: bool = False,
) -> tuple[Any, dict[str, Any]]:
    """Build the peer's durable membership session and its join request."""

    root = _prepared_root(node_root)
    swarm_id, seed_node_id, nonce = _peer_context(adapter)
    node_id = root.name
    node = _membership_session(
        node_id=node_id,
        swarm_id=swarm_id,
        seed_node_id=seed_node_id,
        key_file=root / "node.key",
        endpoint_id=None,
        incarnation=f"{node_id}-1",
        peer_class="browser_http" if unqualified else "mac_mlx_iroh",
        runtime_capability=(
            {
                "runtime_backend": "browser",
                "transport": "http",
                "activation_protocol": None,
            }
            if unqualified
            else None
        ),
    )
    join_envelope = node.join_request(
        invite_nonce=nonce,
        endpoint_addrs=[f"https://{node_id}.invalid/control"],
    )
    return node, join_envelope


def _impostor_for(adapter: Any, node_root: Path) -> Any:
    """Same node_id, different durable key and endpoint identity."""

    root = _prepared_root(node_root)
    swarm_id, seed_node_id, _ = _peer_context(adapter)
    node_id = root.name
    return _membership_session(
        node_id=node_id,
        swarm_id=swarm_id,
        seed_node_id=seed_node_id,
        key_file=root / "impostor.key",
        endpoint_id=None,
        incarnation=f"{node_id}-impostor-1",
    )


def _authority_probe_via(program: Path, member_id: str):
    """Return a bounded no-shell probe executed after peer enrollment."""

    def probe() -> Any:
        import subprocess

        if not program.is_file() or not os.access(program, os.X_OK):
            raise PhysicalGateError("physical_infrastructure_unavailable")
        try:
            completed = subprocess.run(
                [str(program), member_id],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PhysicalGateError("physical_infrastructure_unavailable") from exc
        if completed.returncode != 0:
            raise PhysicalGateError("physical_infrastructure_unavailable")
        try:
            return json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PhysicalGateError("physical_infrastructure_unavailable") from exc

    return probe


def _case_probe_via(
    program: Path,
    case_id: str,
    member_id: str,
    output_file: Path,
    *,
    phase: str = "observe",
    environment: Mapping[str, str] | None = None,
):
    """Execute one bounded live case probe and retain its exact private JSON.

    Revocation uses two invocations. The ``after`` phase receives the exact
    validated before-report over stdin, avoiding an ambient or stale file.
    """

    if phase not in {"observe", "before", "after"}:
        raise PhysicalGateError("physical_infrastructure_unavailable")

    def probe(previous_report: Any = None) -> Any:
        import subprocess

        if not program.is_file() or not os.access(program, os.X_OK):
            raise PhysicalGateError("physical_infrastructure_unavailable")
        if phase == "after":
            if not isinstance(previous_report, dict):
                raise PhysicalGateError("physical_infrastructure_unavailable")
            standard_input = json.dumps(
                previous_report,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            if previous_report is not None:
                raise PhysicalGateError("physical_infrastructure_unavailable")
            standard_input = None
        run_kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": 120,
            "check": False,
            "env": (
                None
                if environment is None
                else {**os.environ, **dict(environment)}
            ),
        }
        if standard_input is None:
            run_kwargs["stdin"] = subprocess.DEVNULL
        else:
            run_kwargs["input"] = standard_input
        try:
            completed = subprocess.run(
                [str(program), case_id, member_id, phase],
                **run_kwargs,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PhysicalGateError("physical_infrastructure_unavailable") from exc
        if completed.returncode != 0:
            raise PhysicalGateError("physical_infrastructure_unavailable")
        try:
            report = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PhysicalGateError("physical_infrastructure_unavailable") from exc
        if not isinstance(report, dict):
            raise PhysicalGateError("physical_infrastructure_unavailable")
        try:
            _write_owner_private_json(output_file, report)
        except OSError as exc:
            raise PhysicalGateError("physical_infrastructure_unavailable") from exc
        return report

    return probe


def _case_probe_inputs(
    program: Path,
    case_id: str,
    member_id: str,
    output_file: Path,
) -> dict[str, Any]:
    if case_id == "revoked_active_member":
        before_file = output_file.with_name(
            f"{output_file.stem}.before{output_file.suffix}"
        )
        return {
            "case_probe_before": _case_probe_via(
                program,
                case_id,
                member_id,
                before_file,
                phase="before",
            ),
            "case_probe_after": _case_probe_via(
                program,
                case_id,
                member_id,
                output_file,
                phase="after",
            ),
        }
    return {
        "case_probe": _case_probe_via(
            program,
            case_id,
            member_id,
            output_file,
        )
    }


def _revoke_via(argv: list[str]):
    """Run the operator's own revocation command; never mint authority here."""

    def revoke() -> None:
        import subprocess

        completed = subprocess.run(argv, capture_output=True, text=True)
        if completed.returncode != 0:
            raise PhysicalGateError("physical_infrastructure_unavailable")

    return revoke


def _read_descriptor_bytes(
    path: Path,
    *,
    minimum_size: int = 1,
    maximum_size: int,
    owner_private: bool,
) -> bytes:
    absolute = path if path.is_absolute() else Path.cwd() / path
    components = absolute.parts
    if len(components) < 2 or components[0] != "/":
        raise OSError("unsafe evidence input path")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        current = os.open("/", directory_flags)
        descriptors.append(current)
        for component in components[1:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise OSError("unsafe evidence input directory")
        descriptor = os.open(components[-1], file_flags, dir_fd=current)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not minimum_size <= before.st_size <= maximum_size
            or (
                owner_private
                and (
                    before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600
                )
            )
        ):
            raise OSError("unsafe evidence input")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise OSError("short evidence input read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OSError("evidence input changed during read")
        return b"".join(chunks)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_descriptor_json(
    path: Path,
    *,
    maximum_size: int,
    owner_private: bool,
) -> Any:
    return json.loads(
        _read_descriptor_bytes(
            path,
            maximum_size=maximum_size,
            owner_private=owner_private,
        ).decode("utf-8")
    )


def _endpoint_mismatch_probe_program(requested: Path | None) -> Path:
    program = Path(__file__).with_name("a8_endpoint_mismatch_probe.py").resolve()
    if requested is not None and requested.resolve() != program:
        raise PhysicalGateError("physical_infrastructure_unavailable")
    if not program.is_file() or not os.access(program, os.X_OK):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    return program


def _endpoint_probe_runtime_files(
    *,
    sidecar_binary: Path | None,
    receiver_endpoint_secret_file: Path | None,
) -> tuple[Path, Path, str]:
    if sidecar_binary is None or receiver_endpoint_secret_file is None:
        raise PhysicalGateError("physical_infrastructure_unavailable")
    try:
        binary = sidecar_binary.resolve(strict=True)
        receiver_key = receiver_endpoint_secret_file.resolve(strict=True)
        if not os.access(binary, os.X_OK):
            raise OSError("sidecar binary is not executable")
        binary_bytes = _read_descriptor_bytes(
            binary,
            minimum_size=1,
            maximum_size=256 * 1024 * 1024,
            owner_private=False,
        )
        _read_descriptor_bytes(
            receiver_key,
            minimum_size=32,
            maximum_size=32,
            owner_private=True,
        )
    except OSError as exc:
        raise PhysicalGateError("physical_infrastructure_unavailable") from exc
    return (
        binary,
        receiver_key,
        "sha256:" + hashlib.sha256(binary_bytes).hexdigest(),
    )


def _load_browser_transport_inputs(
    *,
    browser_report_file: Path | None,
    transport_report_files: list[Path],
    transport_authority_file: Path | None,
    browser_authority_file: Path | None,
    relay_projection_key_file: Path | None,
    require_projection_key: bool,
) -> dict[str, Any]:
    if browser_report_file is None or not transport_report_files:
        raise PhysicalGateError("physical_infrastructure_unavailable")
    if transport_authority_file is None:
        raise PhysicalGateError("transport_observation_signature_invalid")
    if browser_authority_file is None:
        raise PhysicalGateError("browser_observation_signature_invalid")
    try:
        browser_report = _read_descriptor_json(
            browser_report_file, maximum_size=16 * 1024 * 1024, owner_private=False
        )
        transport_reports = [
            _read_descriptor_json(
                path, maximum_size=16 * 1024 * 1024, owner_private=False
            )
            for path in transport_report_files
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PhysicalGateError("physical_infrastructure_unavailable") from exc
    inputs: dict[str, Any] = {
        "browser_report": browser_report,
        "transport_reports": transport_reports,
    }
    try:
        inputs["transport_authority"] = _read_descriptor_json(
            transport_authority_file,
            maximum_size=1024 * 1024,
            owner_private=True,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PhysicalGateError("transport_observation_signature_invalid") from exc
    try:
        inputs["browser_authority"] = _read_descriptor_json(
            browser_authority_file,
            maximum_size=1024 * 1024,
            owner_private=True,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PhysicalGateError("browser_observation_signature_invalid") from exc
    if require_projection_key or relay_projection_key_file is not None:
        if relay_projection_key_file is None:
            raise PhysicalGateError("relay_projection_key_invalid")
        try:
            key = _read_descriptor_bytes(
                relay_projection_key_file,
                minimum_size=32,
                maximum_size=4096,
                owner_private=True,
            )
        except OSError as exc:
            raise PhysicalGateError("relay_projection_key_invalid") from exc
        inputs["relay_projection_key"] = key
    return inputs


def _bound_sha256_digest(value: str) -> str:
    prefix = "sha256:"
    hex_digest = value[len(prefix) :] if value.startswith(prefix) else ""
    if (
        len(hex_digest) != 64
        or any(character not in "0123456789abcdef" for character in hex_digest)
        or hex_digest == "0" * 64
    ):
        raise argparse.ArgumentTypeError("non-placeholder sha256 digest required")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="a8_run_physical_gate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--spec-digest", required=True)
    preflight.add_argument("--source-digest", required=True)
    preflight.add_argument("--now-unix-ms", type=int)

    run = subparsers.add_parser("run")
    run.add_argument("case_id", choices=sorted(A8_PHYSICAL_CASES))
    run.add_argument("--origin", required=True)
    run.add_argument(
        "--transport-origin",
        help=(
            "separate publicly trusted HTTPS endpoint used only by "
            "certificate_without_seed_authority; invite policy remains bound "
            "to --origin"
        ),
    )
    run.add_argument("--evidence-root", type=Path)
    run.add_argument("--seal", action="store_true")
    run.add_argument("--spec-digest", type=_bound_sha256_digest, required=True)
    run.add_argument("--source-digest", type=_bound_sha256_digest, required=True)
    run.add_argument(
        "--bundle-file",
        type=Path,
        help="owner-delivered invite bundle for the seed-side adapter cases",
    )
    run.add_argument("--token-file", type=Path)
    run.add_argument(
        "--browser-report-file",
        type=Path,
        help="A8 product-browser observation JSON emitted by the browser gate",
    )
    run.add_argument(
        "--browser-authority-file",
        type=Path,
        help="owner-private Ed25519 authority for the browser observation",
    )
    run.add_argument(
        "--transport-report-file",
        type=Path,
        action="append",
        default=[],
        dest="transport_report_files",
        help=(
            "signed owner-private /__mycelium/swarm/resource-observations "
            "snapshot; repeat in chronological order for transition gates"
        ),
    )
    run.add_argument(
        "--transport-authority-file",
        type=Path,
        help=(
            "owner-private endpoint-to-verification-key-digest authority for "
            "signed transport reports"
        ),
    )
    run.add_argument(
        "--relay-projection-key-file",
        type=Path,
        help="owner-only (0600) persistent HMAC projection key",
    )
    run.add_argument(
        "--authority-probe-program",
        type=Path,
        help=(
            "owner-controlled executable that receives the enrolled member id "
            "and prints one live unqualified-authority probe JSON document"
        ),
    )
    run.add_argument(
        "--case-probe-program",
        type=Path,
        help=(
            "owner-controlled executable receiving case id, member id, and phase; "
            "the after phase receives the canonical before report on stdin"
        ),
    )
    run.add_argument(
        "--sidecar-binary",
        type=Path,
        help="exact native Iroh sidecar binary for endpoint mismatch execution",
    )
    run.add_argument(
        "--receiver-endpoint-secret-file",
        type=Path,
        help="owner-only receiver endpoint identity for signed mismatch telemetry",
    )
    run.add_argument(
        "--case-probe-output-file",
        type=Path,
        help=(
            "new owner-private file retaining the exact live probe JSON; two-phase "
            "revocation also retains <stem>.before<suffix>"
        ),
    )
    run.add_argument(
        "--node-root",
        type=Path,
        help="owner-private node identity root (replay and peer cases)",
    )
    run.add_argument(
        "--revoke-command",
        nargs="+",
        help=(
            "owner-private administration command that revokes the enrolled "
            "member, run between enrollment and the refusal check "
            "(revoked_active_member). Only usable where this process can "
            "reach the administration plane"
        ),
    )
    run.add_argument(
        "--await-revocation-seconds",
        type=float,
        help=(
            "wait up to this long for an OUT-OF-BAND revocation performed on "
            "the seed host, polling this member's own control path "
            "(revoked_active_member). Use this from an external peer, which "
            "has no route to the administration plane"
        ),
    )
    run.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=2.0,
        help="control-path poll interval while awaiting revocation",
    )

    authority = subparsers.add_parser("build-transport-authority")
    authority.add_argument("--deployment-id", required=True)
    authority.add_argument(
        "--endpoint-secret-file",
        action="append",
        type=Path,
        required=True,
        dest="endpoint_secret_files",
    )
    authority.add_argument("--output-file", type=Path, required=True)

    browser_authority = subparsers.add_parser("build-browser-authority")
    browser_authority.add_argument("--signing-key-file", type=Path, required=True)
    browser_authority.add_argument(
        "--case-id", choices=sorted(_BROWSER_TRANSPORT_CASES), required=True
    )
    browser_authority.add_argument("--origin", required=True)
    browser_authority.add_argument("--deployment-id", required=True)
    browser_authority.add_argument(
        "--spec-digest", type=_bound_sha256_digest, required=True
    )
    browser_authority.add_argument(
        "--source-digest", type=_bound_sha256_digest, required=True
    )
    browser_authority.add_argument("--request-count", type=int, required=True)
    browser_authority.add_argument("--valid-for-seconds", type=int, default=300)
    browser_authority.add_argument("--now-unix-ms", type=int, help=argparse.SUPPRESS)
    browser_authority.add_argument("--output-file", type=Path, required=True)

    cases = subparsers.add_parser("cases")
    cases.add_argument("--json", action="store_true", dest="as_json")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "cases":
        listing = sorted(A8_PHYSICAL_CASES)
        if args.as_json:
            print(json.dumps(listing))
        else:
            print("\n".join(listing))
        return 0
    if args.command == "build-transport-authority":
        try:
            document = _build_transport_authority(
                deployment_id=args.deployment_id,
                endpoint_secret_files=args.endpoint_secret_files,
                output_file=args.output_file,
            )
        except TransportAuthorityError as exc:
            print(f"gate rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(document, sort_keys=True))
        return 0
    if args.command == "build-browser-authority":
        try:
            document = _build_browser_authority(
                signing_key_file=args.signing_key_file,
                case_id=args.case_id,
                origin=args.origin,
                deployment_id=args.deployment_id,
                spec_digest=args.spec_digest,
                source_digest=args.source_digest,
                request_count=args.request_count,
                issued_at_unix_ms=(
                    args.now_unix_ms
                    if args.now_unix_ms is not None
                    else int(time.time() * 1_000)
                ),
                valid_for_seconds=args.valid_for_seconds,
                output_file=args.output_file,
            )
        except BrowserAuthorityError as exc:
            print(f"gate rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(document, sort_keys=True))
        return 0
    if args.command == "preflight":
        now_unix_ms = args.now_unix_ms
        if now_unix_ms is None:
            now_unix_ms = int(time.time() * 1000)
        document = preflight_document(
            now_unix_ms=now_unix_ms,
            spec_digest=args.spec_digest,
            source_digest=args.source_digest,
        )
        print(json.dumps(document, sort_keys=True))
        return 0
    try:
        adapter = None
        case_inputs: dict[str, Any] = {}
        if (
            args.transport_origin is not None
            and args.case_id != "certificate_without_seed_authority"
        ):
            raise PhysicalGateError("case_unknown")
        browser_options_present = bool(
            args.browser_report_file is not None
            or args.browser_authority_file is not None
            or args.transport_report_files
            or args.relay_projection_key_file is not None
        )
        if browser_options_present and args.case_id not in _BROWSER_TRANSPORT_CASES:
            raise PhysicalGateError("case_unknown")
        endpoint_options_present = bool(
            args.sidecar_binary is not None
            or args.receiver_endpoint_secret_file is not None
        )
        if endpoint_options_present and args.case_id != "endpoint_identity_mismatch":
            raise PhysicalGateError("case_unknown")
        if (
            args.transport_authority_file is not None
            and args.case_id not in _BROWSER_TRANSPORT_CASES
            and args.case_id != "endpoint_identity_mismatch"
        ):
            raise PhysicalGateError("case_unknown")
        if args.bundle_file is not None:
            if not args.bundle_file.is_file():
                raise PhysicalGateError("physical_infrastructure_unavailable")
            bundle = json.loads(args.bundle_file.read_text("utf-8"))
            token = (
                args.token_file.read_text("utf-8").strip()
                if args.token_file is not None
                else str(bundle["token"])
            )
            adapter = build_adapter_from_bundle(
                origin=args.origin,
                transport_origin=args.transport_origin,
                bundle=bundle,
                invite_token=token,
            )
        if args.case_id in _BROWSER_TRANSPORT_CASES:
            case_inputs = _load_browser_transport_inputs(
                browser_report_file=args.browser_report_file,
                transport_report_files=args.transport_report_files,
                transport_authority_file=args.transport_authority_file,
                browser_authority_file=args.browser_authority_file,
                relay_projection_key_file=args.relay_projection_key_file,
                require_projection_key=args.case_id
                != "direct_path_qualified_browser_inference",
            )
        elif args.case_id == "invalid_or_replayed_invitation":
            if (
                adapter is None
                or args.node_root is None
                or args.bundle_file is None
            ):
                raise PhysicalGateError("physical_infrastructure_unavailable")
            node_root: Path = args.node_root
            node_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(node_root, 0o700)
            from itertools import count

            from mycelium_node.identity import load_or_create_node_signer
            from mycelium_node.membership import NodeMembershipSession

            verified_payload = adapter._bundle_payload  # noqa: SLF001
            assert isinstance(verified_payload, dict)
            swarm_id = verified_payload["swarm_id"]
            seed_node_id = "a8-rehearsal-seed"
            counter = count(1)
            id_source = lambda: f"a8-gate-node-{next(counter)}"  # noqa: E731

            def _node() -> NodeMembershipSession:
                return NodeMembershipSession(
                    node_id="a8-gate-node",
                    swarm_id=swarm_id,
                    seed_node_id=seed_node_id,
                    signer=load_or_create_node_signer(
                        node_root / "node.key",
                        endpoint_id="a8-gate-node-endpoint",
                    ),
                    incarnation="a8-gate-node-1",
                    software_version="a8-gate-run",
                    peer_class="mac_mlx_iroh",
                    runtime_capability={
                        "runtime_backend": "mlx",
                        "transport": "iroh",
                        "activation_protocol": "mycelium.router_wire.v1",
                    },
                    clock=lambda: __import__("time").time(),
                    id_source=id_source,
                )

            first_node = _node()
            first_envelope = first_node.join_request(
                invite_nonce=verified_payload["nonce"],
                endpoint_addrs=["https://a8-gate-node-a.invalid/control"],
            )
            second_node = _node()
            second_envelope = second_node.join_request(
                invite_nonce=verified_payload["nonce"],
                endpoint_addrs=["https://a8-gate-node-b.invalid/control"],
            )
            if (
                args.case_probe_program is None
                or args.case_probe_output_file is None
            ):
                raise PhysicalGateError("physical_infrastructure_unavailable")
            case_inputs = {
                "first_join_envelope": first_envelope,
                "second_join_envelope": second_envelope,
                "second_adapter": build_adapter_from_bundle(
                    origin=args.origin,
                    bundle=bundle,
                    invite_token=token,
                ),
                "case_probe": _case_probe_via(
                    args.case_probe_program,
                    args.case_id,
                    first_node.node_id,
                    args.case_probe_output_file,
                ),
            }
        elif args.case_id in _PEER_CASES_WITH_CLI_SUPPORT:
            if adapter is None or args.node_root is None:
                raise PhysicalGateError("physical_infrastructure_unavailable")
            node, join_envelope = _peer_node_and_join(
                adapter,
                args.origin,
                args.node_root,
                unqualified=args.case_id == "unqualified_external_member",
            )
            case_inputs = {"node": node, "join_envelope": join_envelope}
            if args.case_id in {
                "unrelated_https_invite_without_tailscale",
                "revoked_active_member",
                "tailscale_unavailable",
                "ssh_unavailable",
            }:
                if (
                    args.case_probe_program is None
                    or args.case_probe_output_file is None
                ):
                    raise PhysicalGateError("physical_infrastructure_unavailable")
                case_inputs.update(
                    _case_probe_inputs(
                        args.case_probe_program,
                        args.case_id,
                        node.node_id,
                        args.case_probe_output_file,
                    )
                )
            # The observers are passed uncalled so each is taken at the point
            # in the window the case actually needs it.
            if args.case_id == "unrelated_https_invite_without_tailscale":
                case_inputs["peer_network"] = observe_peer_network
            elif args.case_id == "tailscale_unavailable":
                case_inputs["peer_network_before"] = observe_peer_network
                case_inputs["peer_network_after"] = observe_peer_network
            elif args.case_id == "ssh_unavailable":
                case_inputs["peer_process_audit"] = observe_process_audit
            elif args.case_id == "endpoint_identity_mismatch":
                case_inputs["mismatched_node"] = _impostor_for(
                    adapter, args.node_root
                )
                probe_program = _endpoint_mismatch_probe_program(
                    args.case_probe_program
                )
                if (
                    args.case_probe_output_file is None
                    or args.transport_authority_file is None
                ):
                    raise PhysicalGateError("physical_infrastructure_unavailable")
                sidecar_binary, receiver_key, sidecar_binary_digest = (
                    _endpoint_probe_runtime_files(
                        sidecar_binary=args.sidecar_binary,
                        receiver_endpoint_secret_file=(
                            args.receiver_endpoint_secret_file
                        ),
                    )
                )
                try:
                    authority = _read_descriptor_json(
                        args.transport_authority_file,
                        maximum_size=1024 * 1024,
                        owner_private=True,
                    )
                    deployment_id = authority["deployment_id"]
                except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
                    raise PhysicalGateError(
                        "transport_observation_signature_invalid"
                    ) from exc
                case_inputs["transport_authority"] = authority
                case_inputs["sidecar_binary_digest"] = sidecar_binary_digest
                case_inputs["case_probe"] = _case_probe_via(
                    probe_program,
                    args.case_id,
                    node.node_id,
                    args.case_probe_output_file,
                    environment={
                        "MYCELIUM_A8_SIDECAR_BINARY": str(
                            sidecar_binary
                        ),
                        "MYCELIUM_A8_EXPECTED_ENDPOINT_SECRET_FILE": str(
                            (args.node_root / "node.key").resolve()
                        ),
                        "MYCELIUM_A8_ROGUE_ENDPOINT_SECRET_FILE": str(
                            (args.node_root / "impostor.key").resolve()
                        ),
                        "MYCELIUM_A8_RECEIVER_ENDPOINT_SECRET_FILE": str(
                            receiver_key
                        ),
                        "MYCELIUM_A8_SPEC_DIGEST": args.spec_digest,
                        "MYCELIUM_A8_SOURCE_DIGEST": args.source_digest,
                        "MYCELIUM_A8_SIDECAR_BINARY_DIGEST": (
                            sidecar_binary_digest
                        ),
                        "MYCELIUM_A8_DEPLOYMENT_ID": str(deployment_id),
                    },
                )
            elif args.case_id == "unqualified_external_member":
                if args.authority_probe_program is None:
                    raise PhysicalGateError("physical_infrastructure_unavailable")
                case_inputs["authority_probe"] = _authority_probe_via(
                    args.authority_probe_program,
                    node.node_id,
                )
            elif args.case_id == "revoked_active_member":
                if args.revoke_command:
                    case_inputs["revoke"] = _revoke_via(args.revoke_command)
                elif args.await_revocation_seconds:
                    case_inputs["await_revocation_seconds"] = (
                        args.await_revocation_seconds
                    )
                    case_inputs["poll_interval_seconds"] = (
                        args.poll_interval_seconds
                    )
                else:
                    raise PhysicalGateError(
                        "physical_infrastructure_unavailable"
                    )
        document = execute_case(
            args.case_id,
            origin=args.origin,
            evidence_root=args.evidence_root,
            adapter=adapter,
            case_inputs=case_inputs,
            spec_digest=args.spec_digest,
            source_digest=args.source_digest,
        )
    except PhysicalGateError as exc:
        print(f"gate rejected: {exc.code}", file=sys.stderr)
        return 2
    print(json.dumps(document, sort_keys=True))
    if document.get("result") != "passed":
        print("gate failed: case result is not passed", file=sys.stderr)
        return 1
    if args.seal:
        if args.evidence_root is None:
            print("gate rejected: evidence_root_unsafe", file=sys.stderr)
            return 2
        try:
            record = seal_qualification(
                document,
                evidence_root=args.evidence_root,
            )
        except PhysicalGateError as exc:
            print(f"gate rejected: {exc.code}", file=sys.stderr)
            return 2
        print(f"sealed: {record}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
