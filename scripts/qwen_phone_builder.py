# SPDX-License-Identifier: AGPL-3.0-only
"""Phone-only route preparation: pinned authority, native replay, no desktop load.

A seed-signed preparation binding is a handoff, not a new way to enroll a phone.
The issuer must consult current membership and applied assignment before signing.
No host challenge is ever promoted to a phone load observation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile
import time

import numpy as np

from mycelium_mobile.qwen_stage_manifest import (
    build_stage_manifest_from_safetensors, verify_signed_stage_manifest,
)
from mycelium_membership import MAX_CLOCK_SKEW_SECONDS
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import build_ed25519_verifier
from mycelium_live.phone_stage_session import validate_phone_configure
from physical_inference_qualification import PeerIdentity, QualificationController

BACKEND = "mobile-rust-qwen2"
BINDING_PROTOCOL = "mycelium.phone_builder_authorization.v2"
PROOF_PROTOCOL = "mycelium.phone_authenticated_load_proof.v2"
#: What the seed's preparation binding is, and what it is not. The route
#: generation is the seed's own peer-routing fact; it is recorded, never
#: compared with or substituted for the phone's load generation.
PREPARATION_PROVENANCE = "seed_compiled_assignment_and_route_generation_distinct_from_native_load_generation"
BUILD_PROTOCOL = "mycelium.phone_executor_build.v1"
BUILD_ID_PROTOCOL = "mycelium.mobile_runtime_build.v1"
from mycelium_qualification.phone import (
    LOAD_IDENTITY_PROVENANCE_V2, PhoneLoadProvenanceError, validate_phone_load_provenance,
)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def require(ok, code):
    if not ok:
        raise RuntimeError(code)


def validate_build_receipt(receipt, *, expected_digest, binary, apk, source_root):
    """Check owner/seed-pinned build-receipt bytes, artifacts and clean sources.

    A receipt is trusted build-system provenance, not something reconstructed
    from filenames. Its expected digest comes from the signed authorization.
    The builder does not assert that merely hashing an APK proves its source.
    """
    require(digest(canonical_json_bytes(receipt)) == expected_digest,
            "phone_build_receipt_digest_mismatch")
    fields = {"protocol", "source_commit", "mobile_core_version", "runtime_build_digest",
              "executor_digest", "apk_digest"}
    require(set(receipt) == fields and receipt["protocol"] == BUILD_PROTOCOL,
            "phone_build_receipt_invalid")
    commit = receipt["source_commit"]
    require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit),
            "phone_build_source_invalid")
    require(file_digest(binary) == receipt["executor_digest"], "phone_executor_digest_mismatch")
    require(file_digest(apk) == receipt["apk_digest"], "phone_apk_digest_mismatch")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(source_root), *args], text=True, timeout=15).strip()
    require(git("rev-parse", "HEAD") == commit and not git("status", "--porcelain", "--untracked-files=all"),
            "phone_build_source_mismatch")
    import tomllib
    version = tomllib.loads((Path(source_root) / "native/mobile_core/Cargo.toml").read_text())["package"]["version"]
    require(version == receipt["mobile_core_version"], "phone_build_version_mismatch")
    expected = digest(canonical_json_bytes({"protocol": BUILD_ID_PROTOCOL,
        "source_commit": commit, "mobile_core_version": version}))
    require(expected == receipt["runtime_build_digest"], "phone_build_identity_mismatch")
    return dict(receipt)


class PhoneEvidence:
    """Verify the seed authorization and the original native replies.

    The load generation is the phone's own signed count (v2 descriptor); a
    record from a producer that only echoed the controller is refused. The
    manifest digest remains a controller claim here: bind_assignment and
    bind_packaged establish it externally, against the seed-compiled
    assignment and the packaged model bytes.

    ``preparation_window=False`` is for final qualification and renewal only:
    the authorization's 600 second window was a preparation-time check, so its
    signature and content are verified but its expiry is not, and the caller
    MUST take freshness from the current seed rows instead.
    """
    def __init__(self, document, *, seed_key_digest, node, run_id, deployment_id, now=None,
                 preparation_window=True):
        now = time.time() if now is None else now
        envelope = document["authorization"]
        key = envelope["verification_key"]
        require(key.get("verification_key_digest") == seed_key_digest,
                "phone_authority_key_mismatch")
        statement = envelope["message"]
        require(envelope.get("protocol") == "mycelium.membership.signed_message.v1"
                and build_ed25519_verifier([key])(canonical_json_bytes(statement), envelope["signature"]) is True,
                "phone_authority_signature_invalid")
        fields = {"protocol", "node_id", "endpoint_id", "incarnation", "membership_generation",
                  "member_verification_key", "run_id", "deployment_id", "issued_at", "expires_at",
                  "configure", "build_receipt_digest", "controller_endpoint_id", "preparation_binding"}
        require(set(statement) == fields and statement["protocol"] == BINDING_PROTOCOL,
                "phone_authority_invalid")
        issued, expires = statement["issued_at"], statement["expires_at"]
        # issued_at is the seed's clock and this may run on the controller
        # host: allow MAX_CLOCK_SKEW_SECONDS ahead; expiry gets no allowance.
        require(type(issued) in (int, float) and type(expires) in (int, float)
                and math.isfinite(issued) and math.isfinite(expires)
                and issued <= now + MAX_CLOCK_SKEW_SECONDS and 0 < expires - issued <= 600
                and (now < expires or preparation_window is False),
                "phone_authority_stale")
        require(all(statement[k] == v for k, v in {
            "node_id": node["node_id"], "endpoint_id": node["endpoint_id"],
            "membership_generation": node["membership_generation"], "run_id": run_id,
            "deployment_id": deployment_id}.items()), "phone_authority_identity_mismatch")
        require(type(statement["membership_generation"]) is int
                and statement["membership_generation"] > 0
                and isinstance(statement["incarnation"], str)
                and statement["incarnation"].startswith("inc-"), "phone_authority_identity_mismatch")
        self.profile = validate_phone_configure(statement["configure"])
        self.manifest = verify_signed_stage_manifest(document["manifest"], key)
        require(self.manifest["golden"] is not None, "phone_golden_required")
        require(self.manifest["stage_id"] == self.profile["stage_id"]
                and self.manifest["pack_digest"] == self.profile["pack_digest"],
                "phone_manifest_profile_mismatch")
        binding = statement['preparation_binding']
        from mycelium_seed.route_assignment import validate_preparation_assignment
        validate_preparation_assignment(binding['assignment'], document['manifest'], node['node_id'])
        # The seed's route generation is carried as its own fact. It is NOT the
        # load generation and no equality between the two is required or used.
        require(set(binding) == {'assignment', 'route_generation', 'provenance'}
                and binding['provenance'] == PREPARATION_PROVENANCE
                and binding['assignment']['assignment_id'] == self.profile['assignment_id']
                and binding['assignment']['manifest_digest'] == self.profile['manifest_digest']
                and binding['assignment']['deployment_id'] == deployment_id
                and type(binding['route_generation']) is int and binding['route_generation'] > 0,
                'phone_preparation_binding_mismatch')
        self.receipt = document["build_receipt"]
        require(digest(canonical_json_bytes(self.receipt)) == statement["build_receipt_digest"],
                "phone_build_receipt_digest_mismatch")
        self.statement = statement
        self.document = json.loads(canonical_json_bytes(document))
        self.peer = PeerIdentity(node_id=node["node_id"], host_id=node["node_id"],
            boot_id=statement["incarnation"], staging_root=node["staging_root"],
            ssh_target=None, process_transport="external_iroh")
        validator = object.__new__(QualificationController)
        observations = []
        for event, field in (("configured", "configured"), ("snapshot", "snapshot")):
            observation = validator._verified_observation(document[field], event=event, peer=self.peer,
                process_id=self.peer.boot_id, run_id=run_id, deployment_id=deployment_id,
                endpoint_id=node["endpoint_id"], expected_verification_key=statement["member_verification_key"])
            observations.append(observation)
        configured, snapshot = observations
        require(configured["state"] == "CONFIGURED" and snapshot["state"] in {"CONFIGURED", "RUNNING"}
                and snapshot["monotonic_ns"] >= configured["monotonic_ns"], "phone_load_state_invalid")
        details = configured["details"]
        expected = {k: v for k, v in self.profile.items() if k != "pack_digest"}
        expected.update(phone_stage_pack_digest=self.profile["pack_digest"],
                        runtime_mode="complete_context_replay")
        require(all(details.get(k) == v for k, v in expected.items())
                and details.get("endpoint_addr", {}).get("id") == node["endpoint_id"],
                "phone_configured_binding_mismatch")
        # These fields must be signed by the native producer. Old producers fail
        # closed; do not fill them from the host challenge or controller profile.
        require(details.get("runtime_build_digest") == self.receipt["runtime_build_digest"]
                and details.get("golden_output_digest") == self.manifest["golden"]["output_digest"]
                and details.get("runtime_backend") == BACKEND,
                "phone_authenticated_load_provenance_required")
        validate_phone_load_provenance(configured)
        resources = snapshot["details"].get("host_resources", {})
        require(resources.get("supported_decode_modes") == ["complete_context_replay"],
                "phone_replay_snapshot_required")
        # The snapshot of the same session must still name the configured load.
        from mycelium_qualification.phone import require_current_phone_load
        try:
            require_current_phone_load(configured, snapshot)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        self.observations = observations

    @property
    def native_load_generation(self):
        """The phone-signed load count; never the seed's or the controller's."""
        return self.observations[0]["details"]["load_generation"]

    def bind_packaged(self, assignment, *, model_manifest):
        """Bind the claimed manifest digest to the packaged full-model manifest.

        The native stage manifest (seed-signed, and the pack the phone applied)
        names one source Safetensors file by hash and size. That same file
        must be the assignment's only weight file and a file of the packaged
        full-model manifest whose digest the assignment and the claim carry.
        Any other representation (sharded or transformed weights) is an
        unsupported mapping and is refused, never assumed equivalent.
        """
        import model_manifest as mm
        m = self.manifest
        require(isinstance(model_manifest, dict) and mm.verify_manifest_digest(model_manifest)
                and mm.manifest_digest_ref(model_manifest) == assignment["manifest_digest"]
                == self.profile["manifest_digest"]
                and model_manifest.get("model_id") == m["model_id"]
                and model_manifest.get("resolved_commit") == m["resolved_commit"],
                "phone_packaged_manifest_mismatch")
        source = m["source"]
        record = dict(path=source["file"], size_bytes=source["size_bytes"], content_digest=source["sha256"])
        packaged = [dict(path=f["path"], size_bytes=f["size_bytes"],
                         content_digest=f"{f['content_digest']['algorithm']}:{f['content_digest']['value']}")
                    for f in model_manifest.get("files", []) if f.get("path") == source["file"]]
        require(assignment["files"] == [record] and packaged == [record],
                "phone_packaged_source_mapping_unsupported")
        return dict(model_manifest_digest=assignment["manifest_digest"], source_file=record,
                    native_pack_digest=m["pack_digest"])

    def bind_assignment(self, assignment, *, stage_id, snapshot_root, model_manifest):
        m = self.manifest
        require(assignment["runtime"]["backend"] == BACKEND
                and assignment["runtime"]["dtype"] == "float32"
                and assignment["runtime"]["quantization"] == "int8-weight-only",
                "phone_runtime_representation_invalid")
        require(assignment["node_id"] == self.statement["node_id"]
                and assignment["expected_tensor_keys"] == [t["name"] for t in m["tensors"]]
                and assignment["assignment_id"] == self.profile["assignment_id"]
                and stage_id == m["stage_id"] and assignment["model_id"] == m["model_id"]
                and assignment["resolved_commit"] == m["resolved_commit"]
                and assignment["range"]["start_layer"] == m["start_layer"]
                and assignment["range"]["end_layer_exclusive"] == m["end_layer_exclusive"]
                and assignment["manifest_digest"] == self.profile["manifest_digest"],
                "phone_assignment_manifest_mismatch")
        require(assignment == self.statement['preparation_binding']['assignment'], 'phone_preparation_assignment_mismatch')
        # Hash the exact source tensors and config consumed by the host executor;
        # a matching model name/range alone cannot establish R1a.
        rebuilt = build_stage_manifest_from_safetensors(Path(snapshot_root) / "model.safetensors",
            json.loads((Path(snapshot_root) / "config.json").read_text()),
            stage_id=m["stage_id"], model_id=m["model_id"], resolved_commit=m["resolved_commit"],
            start_layer=m["start_layer"], end_layer_exclusive=m["end_layer_exclusive"], golden=m["golden"])
        require(rebuilt == m, "phone_challenge_source_mismatch")
        # The rebuilt manifest hashed these exact bytes; the same hash must be
        # the packaged representation's, not merely an equal model name.
        source_binding = self.bind_packaged(assignment, model_manifest=model_manifest)
        validate_phone_load_provenance(self.observations[0])
        return {
            "protocol": PROOF_PROTOCOL,
            "assignment_digest": digest(canonical_json_bytes(assignment)),
            "node_id": assignment["node_id"], "runtime_backend": BACKEND,
            "runtime": dict(assignment["runtime"]),
            "phone_configure": self.profile, "manifest": self.document["manifest"],
            "authorization": self.document["authorization"],
            "configured": self.document["configured"], "snapshot": self.document["snapshot"],
            "build_receipt": self.receipt, "route_ready": False,
            "load_identity_provenance": LOAD_IDENTITY_PROVENANCE_V2,
            "preparation_binding": self.statement['preparation_binding'],
            "source_binding": source_binding,
            "claim_boundary": "Native applied pack and native stage-load generation within one process incarnation; the full-model manifest digest is a controller claim bound externally through the seed-compiled assignment and the packaged source file, not natively observed; distributed token challenge still required.",
        }


def external_control_descriptor(descriptor):
    """Validate with the production consumer; return paths, never secret bytes."""
    from mycelium_live.phone_stage_session import PhoneStageSessionFactory
    PhoneStageSessionFactory.from_descriptor(descriptor)
    return dict(descriptor)


def bind_phone_graph(graph, phone_evidence):
    """Replace only the generic JSON digest with the verified native identity."""
    if not phone_evidence:
        # A build with no phone node has nothing to bind: the graph is returned
        # as the caller built it, never rebuilt.
        return graph
    from dataclasses import replace
    from mycelium_qualification.phone import phone_load_proof_digest
    return replace(graph, stages=tuple(replace(stage, placements=tuple(
        replace(p, load_proof_digest=phone_load_proof_digest(phone_evidence[p.node_id].observations[0]))
        if p.node_id in phone_evidence else p for p in stage.placements)) for stage in graph.stages))


def preparation_proof_digest(proof):
    from mycelium_qualification.phone import phone_load_proof_digest
    from mycelium_router.layer_builder import layer_load_proof_digest
    protocol = proof.get("protocol")
    if protocol == PROOF_PROTOCOL:
        # Only used after PhoneEvidence has verified the original envelope.
        return phone_load_proof_digest(proof["configured"]["result"]["observation"])
    if isinstance(protocol, str) and protocol.startswith("mycelium.phone_authenticated_load_proof."):
        # A retired phone proof is never rehashed as a generic layer proof.
        raise PhoneLoadProvenanceError("phone_load_provenance_unsupported")
    return layer_load_proof_digest(proof)


def authorized_membership_snapshot(snapshot, *, seed_key_digest, assignments, packs,
                                   graph_document, topology, phone_evidence, now):
    """Never mint replacement seed authority for an externally owned phone."""
    from mycelium_membership.contracts import ASSIGNMENT_OFFER_PROTOCOL, verify_membership_message
    require(snapshot.get("protocol") == "mycelium.controller_membership_snapshot.v1"
            and snapshot.get("seed_key_digest") == seed_key_digest,
            "phone_membership_authority_required")
    offers = snapshot.get("assignment_offers", [])
    require(len(offers) == len(assignments), "phone_membership_assignment_mismatch")
    by_node = {n["node_id"]: n for n in topology}
    seen = set()
    for envelope in offers:
        message = verify_membership_message(envelope, now=now, expected_key_digest=seed_key_digest,
            expected_protocol=ASSIGNMENT_OFFER_PROTOCOL, expected_swarm_id=snapshot["swarm_id"])
        node_id = message["recipient_node_id"]
        require(node_id in by_node and node_id not in seen, "phone_membership_assignment_mismatch")
        seen.add(node_id)
        index = next(i for i, a in enumerate(assignments) if a["node_id"] == node_id)
        assignment, pack = assignments[index], packs[index]
        phone = phone_evidence.get(node_id)
        expected_pack = phone.profile["pack_digest"] if phone else pack["stage_pack_digest"]
        require(message["assignment_id"] == assignment["assignment_id"]
                and message["assignment_digest"] == digest(canonical_json_bytes(assignment))
                and message["stage_pack_digest"] == expected_pack
                and message["graph_digest"] == digest(canonical_json_bytes(graph_document))
                and message["generation"] == by_node[node_id]["membership_generation"]
                and message["deployment_id"] == assignment["deployment_id"]
                and message["deployment_epoch"] == assignment["deployment_epoch"],
                "phone_membership_assignment_mismatch")
        if phone:
            require(message["load_generation"] == phone.native_load_generation,
                    "phone_membership_assignment_mismatch")
        records = {p["node_id"]: p for p in message["peer_endpoint_records"]}
        require(set(records) == set(by_node) - {node_id}, "phone_membership_peers_mismatch")
        require(all(records[n]["endpoint_id"] == by_node[n]["endpoint_id"]
                    and records[n]["membership_generation"] == by_node[n]["membership_generation"]
                    for n in records), "phone_membership_peers_mismatch")
    return json.loads(canonical_json_bytes(snapshot))


class RustStageExecutor:
    """One bounded persistent native executor; private f32 files, hashed replies.

    Call validate_build_receipt before constructing in production. Tests may
    exercise this transport with explicitly synthetic tensors, not APK claims.
    """
    def __init__(self, binary, snapshot, start, end, *, hidden_size, timeout=300.0):
        require(0 <= start < end and 0 < hidden_size and math.isfinite(timeout)
                and 0 < timeout <= 300, "phone_executor_arguments_invalid")
        self.start, self.end, self.hidden_size = start, end, hidden_size
        self.timeout = timeout
        self.records = []
        self._buffer = b""
        self._temporary = tempfile.TemporaryDirectory(prefix="mycelium-phone-challenge-")
        self.root = Path(self._temporary.name)
        self.process = None
        self._stderr = (self.root / "stderr").open("wb")
        try:
            self.process = subprocess.Popen([str(binary), str(snapshot), str(start), str(end)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr)
            ready = self._reply(time.monotonic() + timeout)
            require(ready.get("ready") is True, "phone_executor_not_ready")
        except BaseException:
            self.close()
            raise

    def _reply(self, deadline):
        assert self.process is not None and self.process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                require(remaining > 0 and selector.select(remaining), "phone_executor_deadline_exceeded")
                data = os.read(self.process.stdout.fileno(), 4096)
                require(data, "phone_executor_eof")
                self._buffer += data
                require(len(self._buffer) <= 65536, "phone_executor_reply_too_large")
        line, self._buffer = self._buffer.split(b"\n", 1)
        require(time.monotonic() < deadline, "phone_executor_deadline_exceeded")
        value = json.loads(line)
        require(isinstance(value, dict), "phone_executor_reply_invalid")
        return value

    def execute(self, hidden):
        assert self.process is not None and self.process.stdin is not None
        x = np.asarray(hidden, dtype="<f4")
        require(x.ndim == 3 and x.shape[0] == 1 and 1 <= x.shape[1] <= 1024
                and x.shape[2] == self.hidden_size and np.isfinite(x).all(), "phone_executor_input_invalid")
        inp, out = self.root / "input.f32", self.root / "output.f32"
        inp.write_bytes(x.tobytes(order="C"))
        out.unlink(missing_ok=True)
        deadline = time.monotonic() + self.timeout
        try:
            request = dict(seq=x.shape[1], input=str(inp), output=str(out))
            self.process.stdin.write(canonical_json_bytes(request) + b"\n")
            self.process.stdin.flush()
            reply = self._reply(deadline)
            require(reply.get("ok") is True and reply.get("seq") == x.shape[1]
                    and reply.get("layers") == self.end - self.start, "phone_executor_execution_failed")
            require(out.stat().st_size == x.nbytes, "phone_executor_output_size_mismatch")
            raw = out.read_bytes()
            require(digest(raw) == reply.get("sha256"), "phone_executor_output_digest_mismatch")
            result = np.frombuffer(raw, dtype="<f4").reshape(x.shape).copy()
            require(np.isfinite(result).all(), "phone_executor_output_nonfinite")
            self.records.append({"seq": x.shape[1], "input_digest": digest(x.tobytes()),
                                 "output_digest": digest(raw)})
            return result
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()
        self._stderr.close()
        self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
