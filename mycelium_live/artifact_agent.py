"""Generic member-side HTTPS service for authorized artifact chunks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import ssl
import stat
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from mycelium_node.identity import load_node_signer
from mycelium_qualification.signing import build_ed25519_verifier
from mycelium_swarm_artifacts import (
    AVAILABILITY_BUNDLE_PROTOCOL,
    AVAILABILITY_PROTOCOL,
    sign_availability,
    validate_stage_pack_manifest,
)

from .artifact_transport import (
    ArtifactChunkHTTPServer,
    ArtifactChunkSourceAuthority,
    ArtifactRequestReplayStore,
    ArtifactTransportError,
    create_artifact_chunk_server,
)


AGENT_CONFIG_PROTOCOL = "mycelium.artifact_source_agent_config.v1"
_CONFIG_FIELDS = frozenset(
    {
        "protocol",
        "source_member_id",
        "source_membership_generation",
        "source_identity_key_file",
        "object_root",
        "manifest_inbox_directory",
        "provisioner_generation",
        "provisioner_verification_keys",
        "recipient_authorities",
        "listen_host",
        "listen_port",
        "tls_certificate_file",
        "tls_private_key_file",
        "replay_state_root",
        "availability_output_file",
        "advertisement_ttl_seconds",
        "max_concurrent_streams",
        "max_bytes_per_second",
        "serving_priority",
        "transfer_health",
    }
)
_RECIPIENT_FIELDS = frozenset(
    {"member_id", "membership_generation", "verification_key"}
)


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
    ).encode("utf-8")


def _regular_file(path: object, code: str, *, private: bool = False) -> Path:
    if not isinstance(path, str):
        raise ArtifactTransportError(code)
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ArtifactTransportError(code)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ArtifactTransportError(code) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or (private and stat.S_IMODE(metadata.st_mode) != 0o600)
    ):
        raise ArtifactTransportError(code)
    return candidate


def _private_directory(path: object, code: str) -> Path:
    if not isinstance(path, str):
        raise ArtifactTransportError(code)
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ArtifactTransportError(code)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ArtifactTransportError(code) from exc
    if (
        resolved != candidate
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ArtifactTransportError(code)
    return candidate


def _private_output(path: object, code: str) -> Path:
    if not isinstance(path, str):
        raise ArtifactTransportError(code)
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.parent.is_dir():
        raise ArtifactTransportError(code)
    metadata = candidate.parent.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ArtifactTransportError(code)
    if candidate.exists() and (
        candidate.is_symlink()
        or not candidate.is_file()
        or candidate.stat().st_uid != os.geteuid()
    ):
        raise ArtifactTransportError(code)
    return candidate


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _atomic_private_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(_canonical(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class ArtifactSourceAgent:
    """Running source service plus its signed discovery projection."""

    def __init__(
        self,
        *,
        server: ArtifactChunkHTTPServer,
        availability_bundle: Mapping[str, Any],
        availability_output_file: Path,
        reconcile: Callable[[], Mapping[str, Any] | None],
    ) -> None:
        self.server = server
        self.availability_bundle = dict(availability_bundle)
        self.availability_output_file = availability_output_file
        self._reconcile = reconcile

    def publish_availability(self) -> None:
        _atomic_private_json(
            self.availability_output_file, self.availability_bundle
        )

    def reconcile(self) -> bool:
        """Atomically publish a newly registered manifest snapshot, if changed."""

        bundle = self._reconcile()
        if bundle is None:
            return False
        self.availability_bundle = dict(bundle)
        self.publish_availability()
        return True


def _manifest_inbox_snapshot(
    inbox: Path,
) -> tuple[tuple[tuple[str, int, int, int, int], ...], dict[str, dict[str, Any]]]:
    try:
        entries = sorted(inbox.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ArtifactTransportError("artifact_manifest_inbox_unsafe") from exc
    if len(entries) > 1_024:
        raise ArtifactTransportError("artifact_manifest_inbox_unsafe")
    fingerprint: list[tuple[str, int, int, int, int]] = []
    manifests: dict[str, dict[str, Any]] = {}
    for path in entries:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
        except OSError as exc:
            raise ArtifactTransportError("artifact_manifest_inbox_unsafe") from exc
        if (
            path.suffix != ".json"
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > 16 * 1024 * 1024
        ):
            os.close(descriptor)
            raise ArtifactTransportError("artifact_manifest_inbox_unsafe")
        try:
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                encoded = source.read(16 * 1024 * 1024 + 1)
                final_metadata = os.fstat(source.fileno())
            if (
                len(encoded) != metadata.st_size
                or final_metadata.st_ino != metadata.st_ino
                or final_metadata.st_size != metadata.st_size
                or final_metadata.st_mtime_ns != metadata.st_mtime_ns
                or final_metadata.st_ctime_ns != metadata.st_ctime_ns
            ):
                raise ArtifactTransportError("artifact_manifest_inbox_unsafe")
            raw = json.loads(encoded)
            manifest = validate_stage_pack_manifest(raw)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise ArtifactTransportError("artifact_manifest_invalid") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if encoded != _canonical(manifest):
            raise ArtifactTransportError("artifact_manifest_invalid")
        digest = manifest["manifest_digest"]
        if path.name != digest.removeprefix("sha256:") + ".json":
            raise ArtifactTransportError("artifact_manifest_invalid")
        if digest in manifests:
            raise ArtifactTransportError("artifact_manifest_duplicate")
        manifests[digest] = manifest
        fingerprint.append(
            (
                path.name,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
        )
    return tuple(fingerprint), manifests


def _availability_snapshot(
    *,
    manifests: Mapping[str, Mapping[str, Any]],
    object_root: Path,
    source: str,
    generation: int,
    signer: Any,
    config: Mapping[str, Any],
    now: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    availabilities: dict[str, dict[str, Any]] = {}
    for manifest in manifests.values():
        valid_until = min(
            manifest["expires_at_unix_ms"],
            now + config["advertisement_ttl_seconds"] * 1_000,
        )
        if valid_until <= now:
            # Expired inbox records are inert historical control material. Reject
            # their authority before touching large model objects so retries do not
            # turn obsolete manifests into recurring storage work.
            continue
        available = []
        verified_bytes = 0
        for chunk in manifest["chunks"]:
            path = object_root / chunk["content_digest"].removeprefix("sha256:")
            try:
                verified = (
                    path.is_file()
                    and not path.is_symlink()
                    and path.stat().st_uid == os.geteuid()
                    and path.stat().st_size == chunk["size_bytes"]
                    and _digest(path) == chunk["content_digest"]
                )
            except OSError:
                verified = False
            if verified:
                available.append(chunk["content_digest"])
                verified_bytes += chunk["size_bytes"]
        identity = {
            "source_member_id": source,
            "membership_generation": generation,
            "manifest_digest": manifest["manifest_digest"],
            "available_chunk_digests": sorted(available),
            "verified_bytes": verified_bytes,
            "max_concurrent_streams": config["max_concurrent_streams"],
            "max_bytes_per_second": config["max_bytes_per_second"],
            "serving_priority": config["serving_priority"],
            "transfer_health": config["transfer_health"],
        }
        statement = {
            "protocol": AVAILABILITY_PROTOCOL,
            "advertisement_id": "advertisement-"
            + hashlib.sha256(_canonical(identity)).hexdigest()[:32],
            **identity,
            "observed_at_unix_ms": now,
            "valid_until_unix_ms": valid_until,
        }
        availabilities[manifest["manifest_digest"]] = sign_availability(
            statement, signer
        )
    bundle = {
        "protocol": AVAILABILITY_BUNDLE_PROTOCOL,
        "source_member_id": source,
        "membership_generation": generation,
        "advertisements": sorted(
            availabilities.values(), key=lambda item: item["manifest_digest"]
        ),
        "published_at_unix_ms": now,
    }
    return availabilities, bundle


def load_artifact_source_agent(
    config_file: Path,
    *,
    now_unix_ms: int | None = None,
) -> ArtifactSourceAgent:
    fixed_clock = now_unix_ms is not None
    config_path = _regular_file(str(config_file), "artifact_agent_config_unsafe", private=True)
    try:
        raw = json.loads(config_path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactTransportError("artifact_agent_config_invalid") from exc
    if not isinstance(raw, Mapping) or set(raw) != _CONFIG_FIELDS:
        raise ArtifactTransportError("artifact_agent_config_invalid")
    config = dict(raw)
    if config["protocol"] != AGENT_CONFIG_PROTOCOL:
        raise ArtifactTransportError("artifact_agent_config_invalid")
    source = config["source_member_id"]
    generation = config["source_membership_generation"]
    provisioner_generation = config["provisioner_generation"]
    if (
        not isinstance(source, str)
        or not source
        or type(generation) is not int
        or generation < 1
        or type(provisioner_generation) is not int
        or provisioner_generation < 1
    ):
        raise ArtifactTransportError("artifact_agent_config_invalid")
    numeric_bounds = {
        "listen_port": (0, 65_535),
        "advertisement_ttl_seconds": (1, 86_400),
        "max_concurrent_streams": (1, 1_024),
        "max_bytes_per_second": (1, 2**63 - 1),
        "serving_priority": (0, 1_000_000),
    }
    for field, (minimum, maximum) in numeric_bounds.items():
        value = config[field]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ArtifactTransportError("artifact_agent_config_invalid")
    if config["transfer_health"] not in {"healthy", "degraded", "unavailable"}:
        raise ArtifactTransportError("artifact_agent_config_invalid")
    if not isinstance(config["listen_host"], str) or not config["listen_host"]:
        raise ArtifactTransportError("artifact_agent_config_invalid")

    object_root = _private_directory(
        config["object_root"], "artifact_source_root_unsafe"
    )
    replay_root_value = config["replay_state_root"]
    if not isinstance(replay_root_value, str) or not Path(replay_root_value).is_absolute():
        raise ArtifactTransportError("artifact_replay_root_unsafe")
    replay_root = Path(replay_root_value)
    output_file = _private_output(
        config["availability_output_file"], "artifact_availability_output_unsafe"
    )
    identity_file = _regular_file(
        config["source_identity_key_file"], "artifact_source_identity_unsafe", private=True
    )
    certificate = _regular_file(
        config["tls_certificate_file"], "artifact_tls_certificate_unsafe"
    )
    private_key = _regular_file(
        config["tls_private_key_file"], "artifact_tls_private_key_unsafe", private=True
    )
    inbox_value = config["manifest_inbox_directory"]
    if not isinstance(inbox_value, str):
        raise ArtifactTransportError("artifact_manifest_inbox_unsafe")
    inbox = _private_directory(
        inbox_value, "artifact_manifest_inbox_unsafe"
    )

    provisioner_keys = config["provisioner_verification_keys"]
    recipients = config["recipient_authorities"]
    if not isinstance(provisioner_keys, list) or not provisioner_keys:
        raise ArtifactTransportError("artifact_agent_config_invalid")
    if not isinstance(recipients, list) or not recipients or len(recipients) > 4_096:
        raise ArtifactTransportError("artifact_agent_config_invalid")
    recipient_verifiers = {}
    for record in recipients:
        if not isinstance(record, Mapping) or set(record) != _RECIPIENT_FIELDS:
            raise ArtifactTransportError("artifact_agent_config_invalid")
        key = (record["member_id"], record["membership_generation"])
        if (
            not isinstance(key[0], str)
            or not key[0]
            or type(key[1]) is not int
            or key[1] < 1
            or key in recipient_verifiers
        ):
            raise ArtifactTransportError("artifact_agent_config_invalid")
        try:
            recipient_verifiers[key] = build_ed25519_verifier(
                [record["verification_key"]]
            )
        except ValueError as exc:
            raise ArtifactTransportError("artifact_agent_config_invalid") from exc
    try:
        provisioner_verifier = build_ed25519_verifier(provisioner_keys)
        signer = load_node_signer(identity_file, endpoint_id=f"artifact-source-{source}")
    except (RuntimeError, ValueError) as exc:
        raise ArtifactTransportError("artifact_agent_config_invalid") from exc

    now = int(time.time() * 1_000) if now_unix_ms is None else now_unix_ms
    if type(now) is not int or now < 1:
        raise ArtifactTransportError("artifact_agent_clock_invalid")
    fingerprint, manifests = _manifest_inbox_snapshot(inbox)
    availabilities, bundle = _availability_snapshot(
        manifests=manifests,
        object_root=object_root,
        source=source,
        generation=generation,
        signer=signer,
        config=config,
        now=now,
    )

    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        tls_context.load_cert_chain(certificate, private_key)
    except (OSError, ssl.SSLError) as exc:
        raise ArtifactTransportError("artifact_tls_configuration_invalid") from exc
    authority = ArtifactChunkSourceAuthority(
        source_member_id=source,
        source_membership_generation=generation,
        object_root=object_root,
        manifests={digest: manifests[digest] for digest in availabilities},
        availabilities=availabilities,
        source_signer=signer,
        source_verifier=build_ed25519_verifier([signer.public_key_record()]),
        provisioner_verifier=provisioner_verifier,
        recipient_verifier_source=lambda member, member_generation: recipient_verifiers.get(
            (member, member_generation)
        ),
        provisioner_generation=lambda: provisioner_generation,
        replay_store=ArtifactRequestReplayStore(replay_root),
        clock_unix_ms=(lambda: now) if fixed_clock else None,
    )
    server = create_artifact_chunk_server(
        host=config["listen_host"],
        port=config["listen_port"],
        authority=authority,
        tls_context=tls_context,
        maximum_bytes_per_second=config["max_bytes_per_second"],
    )
    snapshot_fingerprint = fingerprint
    ttl_ms = config["advertisement_ttl_seconds"] * 1_000
    refresh_after_unix_ms = now + max(250, ttl_ms // 2)

    def reconcile() -> Mapping[str, Any] | None:
        nonlocal refresh_after_unix_ms, snapshot_fingerprint
        next_fingerprint, next_manifests = _manifest_inbox_snapshot(inbox)
        reconciled_at = now if fixed_clock else int(time.time() * 1_000)
        if (
            next_fingerprint == snapshot_fingerprint
            and reconciled_at < refresh_after_unix_ms
        ):
            return None
        next_availabilities, next_bundle = _availability_snapshot(
            manifests=next_manifests,
            object_root=object_root,
            source=source,
            generation=generation,
            signer=signer,
            config=config,
            now=reconciled_at,
        )
        authority.replace_authority(
            {
                digest: next_manifests[digest]
                for digest in next_availabilities
            },
            next_availabilities,
        )
        snapshot_fingerprint = next_fingerprint
        refresh_after_unix_ms = reconciled_at + max(250, ttl_ms // 2)
        return next_bundle

    return ArtifactSourceAgent(
        server=server,
        availability_bundle=bundle,
        availability_output_file=output_file,
        reconcile=reconcile,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve assignment-authorized Mycelium artifact chunks over HTTPS."
    )
    parser.add_argument("--config", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    agent = load_artifact_source_agent(args.config)
    agent.publish_availability()
    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    prior = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    agent.server.timeout = 0.5
    try:
        while not stop.is_set():
            try:
                agent.reconcile()
            except ArtifactTransportError as exc:
                print(
                    json.dumps(
                        {
                            "event": "artifact_manifest_reconcile_rejected",
                            "reason_code": exc.code,
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            agent.server.handle_request()
    finally:
        agent.server.server_close()
        for signum, handler in prior.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AGENT_CONFIG_PROTOCOL",
    "AVAILABILITY_BUNDLE_PROTOCOL",
    "ArtifactSourceAgent",
    "build_parser",
    "load_artifact_source_agent",
    "main",
]
