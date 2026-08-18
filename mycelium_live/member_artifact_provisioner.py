"""Recipient-side execution of a closed swarm artifact acquisition job."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import ssl
import stat
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
import uuid

from mycelium_node.identity import NodeIdentityError, load_node_signer
from mycelium_qualification.signing import build_ed25519_verifier
from mycelium_swarm_artifacts import (
    SwarmArtifactContractError,
    validate_availability_bundle,
    validate_grant,
    validate_policy,
    validate_stage_pack_manifest,
)

from .artifact_provisioner import (
    ArtifactAcquisitionStore,
    ArtifactProvisioningError,
    SwarmArtifactProvisioner,
)
from .artifact_transport import ArtifactHTTPSChunkReader, ArtifactTransportError


MEMBER_ACQUISITION_JOB_PROTOCOL = "mycelium.member_artifact_acquisition_job.v1"
_JOB_FIELDS = frozenset(
    {
        "protocol",
        "recipient_member_id",
        "recipient_membership_generation",
        "recipient_identity_key_file",
        "provisioner_generation",
        "provisioner_verification_keys",
        "manifest_file",
        "expected_binding_file",
        "grant_file",
        "sources",
        "tls_ca_file",
        "artifact_store_root",
        "policy",
        "predicted_improvement_ratio",
        "serving_reserve_satisfied",
        "status_output_file",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "member_id",
        "membership_generation",
        "endpoint",
        "verification_key",
        "availability_bundle_file",
    }
)


class MemberArtifactAcquisitionError(RuntimeError):
    """Stable recipient-side job validation or execution failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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


def _absolute_regular_file(
    value: object,
    code: str,
    *,
    private: bool = False,
    maximum_bytes: int = 16 * 1024 * 1024,
) -> Path:
    if not isinstance(value, str):
        raise MemberArtifactAcquisitionError(code)
    candidate = Path(value)
    if not candidate.is_absolute():
        raise MemberArtifactAcquisitionError(code)
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise MemberArtifactAcquisitionError(code) from exc
    if (
        resolved != candidate
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_size > maximum_bytes
        or (private and stat.S_IMODE(metadata.st_mode) != 0o600)
    ):
        raise MemberArtifactAcquisitionError(code)
    return candidate


def _private_directory(value: object, code: str, *, create: bool = False) -> Path:
    if not isinstance(value, str):
        raise MemberArtifactAcquisitionError(code)
    candidate = Path(value)
    if not candidate.is_absolute():
        raise MemberArtifactAcquisitionError(code)
    if create:
        try:
            candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise MemberArtifactAcquisitionError(code) from exc
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise MemberArtifactAcquisitionError(code) from exc
    if (
        resolved != candidate
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise MemberArtifactAcquisitionError(code)
    return candidate


def _json_file(value: object, code: str, *, private: bool = False) -> dict[str, Any]:
    path = _absolute_regular_file(value, code, private=private)
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MemberArtifactAcquisitionError(code) from exc
    if not isinstance(document, dict):
        raise MemberArtifactAcquisitionError(code)
    return document


def _write_status(path_value: object, status: Mapping[str, Any]) -> None:
    if path_value is None:
        return
    if not isinstance(path_value, str):
        raise MemberArtifactAcquisitionError("member_acquisition_status_path_invalid")
    destination = Path(path_value)
    if not destination.is_absolute():
        raise MemberArtifactAcquisitionError("member_acquisition_status_path_invalid")
    _private_directory(
        str(destination.parent), "member_acquisition_status_path_invalid"
    )
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_canonical(status))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except OSError as exc:
        raise MemberArtifactAcquisitionError(
            "member_acquisition_status_write_failed"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _job(path: Path) -> dict[str, Any]:
    document = _json_file(
        str(path), "member_acquisition_job_invalid", private=True
    )
    if set(document) != _JOB_FIELDS or document.get("protocol") != MEMBER_ACQUISITION_JOB_PROTOCOL:
        raise MemberArtifactAcquisitionError("member_acquisition_job_invalid")
    member_id = document.get("recipient_member_id")
    generation = document.get("recipient_membership_generation")
    provisioner_generation = document.get("provisioner_generation")
    sources = document.get("sources")
    predicted = document.get("predicted_improvement_ratio")
    if (
        not isinstance(member_id, str)
        or not member_id
        or type(generation) is not int
        or generation < 1
        or type(provisioner_generation) is not int
        or provisioner_generation < 1
        or not isinstance(sources, list)
        or len(sources) > 1_024
        or type(predicted) not in {int, float}
        or not 0 <= float(predicted) <= 1
        or type(document.get("serving_reserve_satisfied")) is not bool
        or not isinstance(document.get("provisioner_verification_keys"), list)
        or not document["provisioner_verification_keys"]
    ):
        raise MemberArtifactAcquisitionError("member_acquisition_job_invalid")
    identities: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != _SOURCE_FIELDS:
            raise MemberArtifactAcquisitionError("member_acquisition_source_invalid")
        source_id = source.get("member_id")
        source_generation = source.get("membership_generation")
        try:
            endpoint = urlsplit(source.get("endpoint"))
            endpoint_port = endpoint.port
        except (TypeError, ValueError) as exc:
            raise MemberArtifactAcquisitionError(
                "member_acquisition_source_invalid"
            ) from exc
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_id in identities
            or type(source_generation) is not int
            or source_generation < 1
            or not isinstance(source.get("endpoint"), str)
            or not source["endpoint"]
            or not isinstance(source.get("verification_key"), Mapping)
            or endpoint.scheme != "https"
            or endpoint.hostname is None
            or endpoint_port is None
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise MemberArtifactAcquisitionError("member_acquisition_source_invalid")
        identities.add(source_id)
    output = document.get("status_output_file")
    if output is not None and (
        not isinstance(output, str) or not Path(output).is_absolute()
    ):
        raise MemberArtifactAcquisitionError(
            "member_acquisition_status_path_invalid"
        )
    validate_policy(document.get("policy"))
    return document


def acquire_member_stage_pack(
    job_file: Path,
    *,
    clock_unix_ms: Callable[[], int] | None = None,
) -> dict[str, Any]:
    """Execute one recipient-bound job without an implicit coordinator origin."""

    job = _job(Path(job_file))
    request_clock = (
        (lambda: int(time.time() * 1_000))
        if clock_unix_ms is None
        else clock_unix_ms
    )
    now = request_clock()
    if type(now) is not int or now < 1:
        raise MemberArtifactAcquisitionError("member_acquisition_clock_invalid")
    manifest_document = _json_file(
        job["manifest_file"], "member_acquisition_manifest_invalid"
    )
    binding = _json_file(
        job["expected_binding_file"], "member_acquisition_binding_invalid"
    )
    grant_document = _json_file(
        job["grant_file"], "member_acquisition_grant_invalid", private=True
    )
    try:
        manifest = validate_stage_pack_manifest(
            manifest_document, expected_binding=binding
        )
        provisioner_verifier = build_ed25519_verifier(
            job["provisioner_verification_keys"]
        )
        grant = validate_grant(
            grant_document,
            verifier=provisioner_verifier,
            now_unix_ms=now,
            expected_recipient_member_id=job["recipient_member_id"],
            expected_recipient_membership_generation=job[
                "recipient_membership_generation"
            ],
            expected_provisioner_generation=job["provisioner_generation"],
            expected_manifest_digest=manifest["manifest_digest"],
            expected_assignment_digest=manifest["assignment_digest"],
            expected_representation_digest=manifest["representation_digest"],
            expected_feasibility_digest=manifest["feasibility_digest"],
        )
        recipient_identity = _absolute_regular_file(
            job["recipient_identity_key_file"],
            "member_acquisition_identity_invalid",
            private=True,
            maximum_bytes=32,
        )
        recipient_signer = load_node_signer(
            recipient_identity,
            endpoint_id=f"artifact-recipient-{job['recipient_member_id']}",
        )
        endpoints: dict[str, str] = {}
        advertisements: dict[str, dict[str, Any]] = {}
        source_verifiers = {}
        for source in job["sources"]:
            source_id = source["member_id"]
            verifier = build_ed25519_verifier([source["verification_key"]])
            bundle = validate_availability_bundle(
                _json_file(
                    source["availability_bundle_file"],
                    "member_acquisition_availability_invalid",
                ),
                verifier=verifier,
                now_unix_ms=now,
                expected_source_member_id=source_id,
                expected_membership_generation=source["membership_generation"],
            )
            matching = [
                item
                for item in bundle["advertisements"]
                if item["manifest_digest"] == manifest["manifest_digest"]
            ]
            if len(matching) != 1:
                raise MemberArtifactAcquisitionError(
                    "member_acquisition_availability_missing"
                )
            endpoints[source_id] = source["endpoint"]
            advertisements[source_id] = matching[0]
            source_verifiers[source_id] = verifier
        if set(grant["authorized_source_member_ids"]) - set(endpoints):
            raise MemberArtifactAcquisitionError(
                "member_acquisition_authorized_source_missing"
            )
        ca_file = _absolute_regular_file(
            job["tls_ca_file"], "member_acquisition_tls_ca_invalid"
        )
        tls_context = ssl.create_default_context(cafile=str(ca_file))
        tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        reader = ArtifactHTTPSChunkReader(
            endpoints=endpoints,
            availabilities=advertisements,
            source_verifiers=source_verifiers,
            recipient_member_id=job["recipient_member_id"],
            recipient_membership_generation=job[
                "recipient_membership_generation"
            ],
            recipient_signer=recipient_signer,
            tls_context=tls_context,
            clock_unix_ms=request_clock,
        )
        store_root = _private_directory(
            job["artifact_store_root"],
            "member_acquisition_store_invalid",
            create=True,
        )
        store = ArtifactAcquisitionStore(store_root)
        status = SwarmArtifactProvisioner(
            store, clock_unix_ms=request_clock
        ).acquire(
            manifest=manifest,
            expected_binding=binding,
            grant=grant,
            advertisements=list(advertisements.values()),
            policy=job["policy"],
            reader=reader,
            origin=None,
            predicted_improvement_ratio=float(job["predicted_improvement_ratio"]),
            serving_reserve_satisfied=job["serving_reserve_satisfied"],
        )
    except MemberArtifactAcquisitionError:
        raise
    except NodeIdentityError as exc:
        raise MemberArtifactAcquisitionError(
            "member_acquisition_identity_invalid"
        ) from exc
    except (
        ArtifactProvisioningError,
        ArtifactTransportError,
        SwarmArtifactContractError,
        ValueError,
    ) as exc:
        code = getattr(exc, "code", None)
        raise MemberArtifactAcquisitionError(
            code if isinstance(code, str) else "member_acquisition_failed"
        ) from exc
    _write_status(job["status_output_file"], status)
    return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Acquire one assigned stage pack on its recipient member."
    )
    parser.add_argument("--job", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        status = acquire_member_stage_pack(args.job)
    except MemberArtifactAcquisitionError as exc:
        print(
            json.dumps(
                {
                    "protocol": "mycelium.member_artifact_acquisition_failure.v1",
                    "reason_code": exc.code,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "protocol": "mycelium.member_artifact_acquisition_result.v1",
                "status": status,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MEMBER_ACQUISITION_JOB_PROTOCOL",
    "MemberArtifactAcquisitionError",
    "acquire_member_stage_pack",
]
