#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Execute A8's authenticated Iroh endpoint-mismatch probe.

The program emits only node-signed before/after admission snapshots. It is
invoked by ``a8_run_physical_gate.py`` with candidate-bound configuration in
its environment; arbitrary probe executables are not accepted for this case.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_iroh_sidecar import SidecarClient, SidecarError  # noqa: E402
from mycelium_node.identity import (  # noqa: E402
    NodeIdentityError,
    load_node_signer,
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ProbeError(RuntimeError):
    pass


class RunningSidecar:
    def __init__(self, root: Path, *, binary: Path, endpoint_secret: Path) -> None:
        self.root = root
        self.bootstrap_secret = os.urandom(32)
        self.socket_path = root / "run" / "sidecar.sock"
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, self.bootstrap_secret)
        finally:
            os.close(write_fd)
        try:
            self.process = subprocess.Popen(
                [
                    str(binary),
                    "--uds",
                    str(self.socket_path),
                    "--bootstrap-fd",
                    str(read_fd),
                    "--endpoint-secret-file",
                    str(endpoint_secret),
                    "--local-only",
                    "--queue-capacity",
                    "8",
                ],
                cwd=ROOT,
                pass_fds=(read_fd,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        finally:
            os.close(read_fd)
        try:
            self.ready = self._read_ready()
        except BaseException:
            self.close()
            raise

    def _read_ready(self) -> dict[str, Any]:
        if self.process.stdout is None:
            raise ProbeError("sidecar_stdout_unavailable")
        readable, _, _ = select.select([self.process.stdout], [], [], 20)
        if not readable:
            raise ProbeError("sidecar_readiness_timeout")
        line = self.process.stdout.readline()
        if not line:
            raise ProbeError("sidecar_exited_before_ready")
        ready = json.loads(line)
        endpoint_id = ready.get("endpoint_id") if isinstance(ready, dict) else None
        endpoint_addr = ready.get("endpoint_addr") if isinstance(ready, dict) else None
        if (
            not isinstance(ready, dict)
            or set(ready) != {"event", "endpoint_id", "endpoint_addr", "alpn"}
            or ready.get("event") != "ready"
            or ready.get("alpn") != "mycelium.iroh.sidecar.v1"
            or not isinstance(endpoint_id, str)
            or not isinstance(endpoint_addr, dict)
            or set(endpoint_addr) != {"id", "addrs"}
            or endpoint_addr.get("id") != endpoint_id
            or not isinstance(endpoint_addr.get("addrs"), list)
            or not endpoint_addr["addrs"]
        ):
            raise ProbeError("sidecar_readiness_invalid")
        return ready

    def client(self) -> SidecarClient:
        client = SidecarClient(self.socket_path, self.bootstrap_secret, timeout=5)
        client.connect()
        return client

    def close(self) -> None:
        process = getattr(self, "process", None)
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise ProbeError(f"{name.lower()}_missing")
    path = Path(value).resolve()
    if not path.is_file():
        raise ProbeError(f"{name.lower()}_invalid")
    return path


def _required_text(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ProbeError(f"{name.lower()}_missing")
    return value


def _peer(sidecar: RunningSidecar) -> dict[str, Any]:
    return {
        "endpoint_id": sidecar.ready["endpoint_id"],
        "endpoint_addr": sidecar.ready["endpoint_addr"],
        "generation": 1,
    }


def _signed_snapshot(
    *,
    signer: Any,
    run_id: str,
    deployment_id: str,
    member_id: str,
    spec_digest: str,
    source_digest: str,
    sidecar_binary_digest: str,
    challenge: str,
    receiver_endpoint_id: str,
    expected_endpoint_id: str,
    dialed_endpoint_id: str,
    admission: dict[str, Any],
) -> dict[str, Any]:
    observation = {
        "protocol": "mycelium.physical_node_observation.v1",
        "event": "inbound_admission_snapshot",
        "monotonic_ns": time.monotonic_ns(),
        "run_id": run_id,
        "deployment_id": deployment_id,
        "node_id": "a8-endpoint-mismatch-receiver",
        "host_id": "a8-private-probe-host",
        "process_id": os.getpid(),
        "endpoint_id": receiver_endpoint_id,
        "peer_generation": 1,
        "state": "RUNNING",
        "route_ready": False,
        "details": {
            "protocol": "mycelium.physical_node.inbound_admission_evidence.v1",
            "case_id": "endpoint_identity_mismatch",
            "member_id": member_id,
            "spec_digest": spec_digest,
            "source_digest": source_digest,
            "sidecar_binary_digest": sidecar_binary_digest,
            "challenge": challenge,
            "expected_endpoint_id": expected_endpoint_id,
            "dialed_endpoint_id": dialed_endpoint_id,
            "expected_peer_path_class": "unknown",
            "admission": admission,
        },
    }
    return {
        "observation": observation,
        "signature": signer.sign(observation),
        "verification_key": signer.public_key_record(),
    }


def execute(member_id: str) -> dict[str, Any]:
    binary = _required_path("MYCELIUM_A8_SIDECAR_BINARY")
    expected_key = _required_path("MYCELIUM_A8_EXPECTED_ENDPOINT_SECRET_FILE")
    rogue_key = _required_path("MYCELIUM_A8_ROGUE_ENDPOINT_SECRET_FILE")
    receiver_key = _required_path("MYCELIUM_A8_RECEIVER_ENDPOINT_SECRET_FILE")
    spec_digest = _required_text("MYCELIUM_A8_SPEC_DIGEST")
    source_digest = _required_text("MYCELIUM_A8_SOURCE_DIGEST")
    expected_binary_digest = _required_text("MYCELIUM_A8_SIDECAR_BINARY_DIGEST")
    deployment_id = _required_text("MYCELIUM_A8_DEPLOYMENT_ID")
    if any(
        _DIGEST.fullmatch(value) is None
        for value in (spec_digest, source_digest, expected_binary_digest)
    ):
        raise ProbeError("candidate_digest_invalid")
    binary_digest = "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest()
    if binary_digest != expected_binary_digest:
        raise ProbeError("sidecar_binary_digest_mismatch")
    signer = load_node_signer(receiver_key)
    signer_endpoint_id = base64.b64decode(
        signer.public_key_record()["verification_key"], validate=True
    ).hex()
    signer = load_node_signer(receiver_key, endpoint_id=signer_endpoint_id)
    root = Path(tempfile.mkdtemp(prefix="mycelium-a8-mismatch-", dir="/tmp"))
    os.chmod(root, 0o700)
    sidecars: list[RunningSidecar] = []
    clients: list[SidecarClient] = []
    try:
        expected = RunningSidecar(root / "expected", binary=binary, endpoint_secret=expected_key)
        receiver = RunningSidecar(root / "receiver", binary=binary, endpoint_secret=receiver_key)
        rogue = RunningSidecar(root / "rogue", binary=binary, endpoint_secret=rogue_key)
        sidecars.extend((expected, receiver, rogue))
        if signer.endpoint_id != receiver.ready["endpoint_id"]:
            raise ProbeError("receiver_signer_endpoint_mismatch")
        receiver_client = receiver.client()
        rogue_client = rogue.client()
        clients.extend((receiver_client, rogue_client))
        receiver_client.configure_peers([_peer(expected)])
        rogue_client.configure_peers([_peer(receiver)])
        expected_endpoint_id = str(expected.ready["endpoint_id"])
        dialed_endpoint_id = str(rogue.ready["endpoint_id"])
        before_admission = receiver_client.inbound_admission_snapshot(dialed_endpoint_id)
        paths = receiver_client.transport_observations()
        if not any(
            item.get("remote_endpoint_id") == expected_endpoint_id
            and item.get("path_class") == "unknown"
            for item in paths
        ):
            raise ProbeError("expected_path_not_unknown")
        challenge = "a8-endpoint-mismatch-" + os.urandom(16).hex()
        run_id = "a8-endpoint-mismatch-" + os.urandom(8).hex()
        before = _signed_snapshot(
            signer=signer,
            run_id=run_id,
            deployment_id=deployment_id,
            member_id=member_id,
            spec_digest=spec_digest,
            source_digest=source_digest,
            sidecar_binary_digest=binary_digest,
            challenge=challenge,
            receiver_endpoint_id=str(receiver.ready["endpoint_id"]),
            expected_endpoint_id=expected_endpoint_id,
            dialed_endpoint_id=dialed_endpoint_id,
            admission=before_admission,
        )
        frame = (ROOT / "contracts/router-wire-golden/01-hop-header.bin").read_bytes()
        try:
            rogue_client.send_confirmed(
                frame,
                os.urandom(16),
                timeout=2,
                expected_generation=1,
                source_generation=1,
            )
        except (TimeoutError, OSError):
            pass
        deadline = time.monotonic() + 5
        while True:
            after_admission = receiver_client.inbound_admission_snapshot(dialed_endpoint_id)
            if (
                after_admission["candidate_identity_rejections"]
                > before_admission["candidate_identity_rejections"]
            ):
                break
            if time.monotonic() >= deadline:
                raise ProbeError("identity_rejection_not_observed")
            time.sleep(0.05)
        after = _signed_snapshot(
            signer=signer,
            run_id=run_id,
            deployment_id=deployment_id,
            member_id=member_id,
            spec_digest=spec_digest,
            source_digest=source_digest,
            sidecar_binary_digest=binary_digest,
            challenge=challenge,
            receiver_endpoint_id=str(receiver.ready["endpoint_id"]),
            expected_endpoint_id=expected_endpoint_id,
            dialed_endpoint_id=dialed_endpoint_id,
            admission=after_admission,
        )
        if "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest() != binary_digest:
            raise ProbeError("sidecar_binary_changed")
        return {
            "protocol": "mycelium.a8_endpoint_activation_probe.v2",
            "case_id": "endpoint_identity_mismatch",
            "member_id": member_id,
            "spec_digest": spec_digest,
            "source_digest": source_digest,
            "sidecar_binary_digest": binary_digest,
            "challenge": challenge,
            "before": before,
            "after": after,
        }
    finally:
        for client in reversed(clients):
            client.close()
        for sidecar in reversed(sidecars):
            sidecar.close()
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] != "endpoint_identity_mismatch" or argv[3] != "observe":
        return 2
    try:
        report = execute(argv[2])
    except (
        NodeIdentityError,
        OSError,
        ProbeError,
        SidecarError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, allow_nan=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
