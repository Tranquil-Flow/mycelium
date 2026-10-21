# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed work consent for the managed native worker boundary.

Two clearly separated proof classes:

* ``TestMetadataPins`` — pure metadata/binding checks: no native process,
  no model bytes. These are deliberately fake/metadata-only tests.
* ``TestRealNativeBoundary`` — starts the REAL native node process and proves
  the exact-grant gate physically: an admitted, consent-pinned ``configure``
  reaches the real native authoritative validation (which rejects it — zero
  model bytes loaded), while a pin-tampered ``configure`` is denied locally
  with no native call.
"""
import hashlib
import json
import time
from pathlib import Path

import pytest

import model_manifest as mm
from layer_assignment import assignment_id_for
from mycelium_a13_macos.serving import CANDIDATE_MODEL, RESOURCE_PROPOSAL
from mycelium_a13_macos.worker import WorkerLimits
from mycelium_qualification.evidence import canonical_json_bytes

from mycelium_a13_macos.work_authorization import (
    WorkAuthorization,
    WorkAuthorizationError,
    authorize_work_consent,
)

RUN_ID = "run-test"
# Assignment identity requires a canonical UUID deployment namespace; the
# managed worker/offer/native boundary accepts the same value as a segment.
DEPLOYMENT_ID = "11111111-2222-3333-4444-555555555555"
NODE_ID = "mac-test"


def _digest(value):
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _manifest():
    """Synthetic Qwen metadata through the real manifest compiler; no weights."""
    weights = {name: "toy.safetensors" for name in (
        "model.embed_tokens.weight", "model.norm.weight", "lm_head.weight",
        *(f"model.layers.{i}.self_attn.q_proj.weight" for i in range(3)))}
    return mm.compile_model_manifest(
        model_id=CANDIDATE_MODEL.model_id,
        requested_revision=CANDIDATE_MODEL.resolved_commit,
        resolved_commit=CANDIDATE_MODEL.resolved_commit,
        config={"model_type": "qwen2", "architectures": ["Qwen2ForCausalLM"],
                "num_hidden_layers": 3, "hidden_size": 4, "num_attention_heads": 1,
                "num_key_value_heads": 1, "head_dim": 4, "intermediate_size": 8,
                "vocab_size": 8, "max_position_embeddings": 16, "rms_norm_eps": 1e-6,
                "rope_theta": 10000.0, "hidden_act": "silu", "tie_word_embeddings": False},
        checkpoint_index={"metadata": {"total_size": 128}, "weight_map": weights},
        file_metadata={"toy.safetensors": {"size_bytes": 128, "sha256": "1" * 64}},
    )


def _offer(**changes):
    offer = {
        "target_node_id": NODE_ID,
        "target_endpoint_id": "endpoint-test",
        "target_key_digest": "sha256:" + "a" * 64,
        "membership_generation": 1,
        "member_incarnation": "incarnation-1",
        "run_id": RUN_ID,
        "deployment_id": DEPLOYMENT_ID,
        "offer_id": "offer-test",
        "archive_digest": "sha256:" + "b" * 64,
        "archive_bytes": 64,
        "expires_at": time.time() + 300,
    }
    offer.update(changes)
    return offer


def _assignment(staged_root, manifest, *, deployment_id=DEPLOYMENT_ID,
                node_id=NODE_ID, artifact_cache_root=None, runtime=None):
    base = {
        "protocol": "mycelium.layer_assignment.v2",
        "deployment_id": deployment_id,
        "deployment_epoch": 1,
        "node_id": node_id,
        "manifest_digest": mm.manifest_digest_ref(manifest),
        "model_id": CANDIDATE_MODEL.model_id,
        "resolved_commit": CANDIDATE_MODEL.resolved_commit,
        "range": {"start_layer": 0, "end_layer_exclusive": 2},
        "components": ["input_embedding", "decoder"],
        "component_tensor_keys": {},
        "component_aliases": {},
        "expected_tensor_prefixes": [],
        "expected_tensor_keys": [],
        "files": [{"path": "model.safetensors", "size_bytes": 128,
                   "content_digest": {"algorithm": "sha256", "value": "1" * 64}}],
        "artifact_cache_root": artifact_cache_root or str(staged_root / "deployment"),
        "runtime": runtime or {**manifest['runtime_model'],"backend": CANDIDATE_MODEL.runtime_backend,
                               "dtype": CANDIDATE_MODEL.runtime_dtype,
                               "quantization": CANDIDATE_MODEL.runtime_quantization},
    }
    return {**base, "assignment_id": assignment_id_for(base), "route_ready": False}


def _limits(**changes):
    values = dict(max_rss_bytes=512 * 1024 * 1024, max_disk_bytes=100_000,
                  max_wall_seconds=30, max_commands=20)
    values.update(changes)
    return WorkerLimits(**values)


def _consent(tmp_path, **changes):
    staged = (tmp_path / "staged")
    staged.mkdir(exist_ok=True)
    staged = staged.resolve()
    manifest = _manifest()
    assignment = changes.pop("assignment", None) or _assignment(staged, manifest)
    kwargs = dict(
        offer=_offer(),
        acknowledged=True,
        limits=_limits(),
        staged_root=staged,
        assignment=assignment,
        expected_assignment_digest=_digest(assignment),
        manifest=manifest,
    )
    kwargs.update(changes)
    (staged/'control').mkdir(exist_ok=True)
    (staged/'deployment').mkdir(exist_ok=True)
    for path,document in [('assignment.json',assignment),('control/assignment.json',assignment),('deployment/assignment.json',assignment),
        ('manifest.json',manifest),('control/execution-graph.json',_graph(mm.manifest_digest_ref(manifest)))]:
        (staged/path).write_text(json.dumps(document))
    return authorize_work_consent(**kwargs), staged, manifest, assignment


def _graph(manifest_digest, **changes):
    graph = {
        "protocol": "mycelium.execution_graph.v1",
        "deployment_id": DEPLOYMENT_ID,
        "deployment_epoch": 1,
        "model_id": CANDIDATE_MODEL.model_id,
        "resolved_commit": CANDIDATE_MODEL.resolved_commit,
        "manifest_digest": manifest_digest,
        "stages": [{"placements": [{"node_id":n} for n in (NODE_ID,'mac-other','mac-a','mac-b')]}],
    }
    graph.update(changes)
    return graph


def _configure_payload(manifest_digest, **changes):
    payload = {
        "assignment_file": "assignment.json",
        "artifact_report_file": "report.json",
        "graph": _graph(manifest_digest),
        "device_states": {},
        "load_generation": 1,
    }
    payload.update(changes)
    return payload


def _peer(node='mac-other', **changes):
    endpoint=hashlib.sha256(node.encode()).hexdigest()
    value={'node_id':node,'endpoint_id':endpoint,'endpoint_addr':{'id':endpoint},'generation':1}
    value.update(changes)
    return value


def _envelope(command, payload, **changes):
    envelope = {
        "protocol": "mycelium.physical_node_control.v1",
        "command_id": "command-test",
        "run_id": RUN_ID,
        "deployment_id": DEPLOYMENT_ID,
        "command": command,
        "payload": payload,
    }
    envelope.update(changes)
    return envelope


class TestMetadataPins:
    """Fake/metadata-only: no native process, no model bytes."""

    def test_consent_pins_offer_assignment_and_manifest(self, tmp_path):
        authz, staged, manifest, assignment = _consent(tmp_path)
        assert isinstance(authz, WorkAuthorization)
        assert isinstance(authz.limits, WorkerLimits)
        assert callable(authz.authorize_load)
        pinned = authz.pinned()
        assert pinned["model_id"] == CANDIDATE_MODEL.model_id
        assert pinned["resolved_commit"] == CANDIDATE_MODEL.resolved_commit
        assert pinned["manifest_digest"] == mm.manifest_digest_ref(manifest)
        assert pinned["assignment_id"] == assignment["assignment_id"]
        assert pinned["run_id"] == RUN_ID and pinned["deployment_id"] == DEPLOYMENT_ID
        assert pinned["staged_root"] == str(staged)
        assert pinned["grant_commands"] == (
            "configure", "infer_decode", "infer_start", "rotate", "start")

    def test_pinned_candidate_model_unchanged(self):
        # wp8-02 G1 (2026-09-16): the owner-directed 7B useful-model composition.
        assert CANDIDATE_MODEL.model_id == "Qwen/Qwen2.5-7B-Instruct"
        assert CANDIDATE_MODEL.resolved_commit == \
            "a09a35458c702b33eeacc393d103063234e8bc28"
        assert CANDIDATE_MODEL.sampling_seed == 0
        assert (CANDIDATE_MODEL.runtime_backend, CANDIDATE_MODEL.runtime_dtype,
                CANDIDATE_MODEL.runtime_quantization) == \
            ("mlx", "float32", "int8-weight-only")

    def test_missing_acknowledgement_fails_closed(self, tmp_path):
        with pytest.raises(WorkAuthorizationError,
                           match="work_consent_acknowledgement_missing"):
            _consent(tmp_path, acknowledged=False)

    def test_invalid_offer_fails_closed(self, tmp_path):
        broken = {k: v for k, v in _offer().items() if k != "run_id"}
        with pytest.raises(WorkAuthorizationError, match="work_offer_invalid"):
            _consent(tmp_path, offer=broken)

    def test_non_worker_limits_fails_closed(self, tmp_path):
        with pytest.raises(WorkAuthorizationError, match="work_limits_invalid"):
            _consent(tmp_path, limits={"max_rss_bytes": 1})

    @pytest.mark.parametrize("field", ["max_rss_bytes", "max_disk_bytes",
                                       "max_wall_seconds"])
    def test_limits_above_proposal_ceiling_fail_closed(self, tmp_path, field):
        ceiling = {"max_rss_bytes": RESOURCE_PROPOSAL.max_combined_rss_bytes,
                   "max_disk_bytes": RESOURCE_PROPOSAL.additional_disk_bytes,
                   "max_wall_seconds": RESOURCE_PROPOSAL.max_wall_seconds}[field]
        with pytest.raises(WorkAuthorizationError,
                           match="work_limits_exceed_proposal"):
            _consent(tmp_path, limits=_limits(**{field: ceiling + 1}))

    def test_disk_limit_below_offer_archive_fails_closed(self, tmp_path):
        with pytest.raises(WorkAuthorizationError,
                           match="work_limits_below_offer_archive"):
            _consent(tmp_path, limits=_limits(max_disk_bytes=32))

    def test_non_canonical_staged_root_fails_closed(self, tmp_path):
        staged = tmp_path / "staged"
        staged.mkdir()
        with pytest.raises(WorkAuthorizationError, match="work_staged_root_invalid"):
            _consent(tmp_path, staged_root=Path(str(staged.resolve()) + "/sub/.."))

    def test_symlinked_staged_root_fails_closed(self, tmp_path):
        staged = tmp_path / "staged"
        staged.mkdir()
        link = tmp_path / "link"
        link.symlink_to(staged)
        with pytest.raises(WorkAuthorizationError, match="work_staged_root_invalid"):
            _consent(tmp_path, staged_root=link)

    def test_missing_staged_root_fails_closed(self, tmp_path):
        with pytest.raises(WorkAuthorizationError, match="work_staged_root_invalid"):
            _consent(tmp_path, staged_root=tmp_path / "absent")

    def test_assignment_digest_mismatch_fails_closed(self, tmp_path):
        with pytest.raises(WorkAuthorizationError,
                           match="work_assignment_digest_mismatch"):
            _consent(tmp_path, expected_assignment_digest="sha256:" + "0" * 64)

    def test_assignment_wrong_deployment_fails_closed(self, tmp_path):
        staged = (tmp_path / "staged")
        staged.mkdir()
        staged = staged.resolve()
        manifest = _manifest()
        other = _assignment(staged, manifest,
                            deployment_id="99999999-2222-3333-4444-555555555555")
        with pytest.raises(WorkAuthorizationError, match="work_assignment_invalid"):
            _consent(tmp_path, assignment=other,
                     expected_assignment_digest=_digest(other))

    def test_assignment_wrong_node_fails_closed(self, tmp_path):
        staged = (tmp_path / "staged")
        staged.mkdir()
        staged = staged.resolve()
        manifest = _manifest()
        other = _assignment(staged, manifest, node_id="mac-other")
        with pytest.raises(WorkAuthorizationError, match="work_assignment_invalid"):
            _consent(tmp_path, assignment=other,
                     expected_assignment_digest=_digest(other))

    def test_assignment_cache_root_must_match_staged_deployment_path(self, tmp_path):
        # Correction under test: the staged artifact cache root is the
        # offer-bound staged deployment path, NOT the HF source root.
        staged = (tmp_path / "staged")
        staged.mkdir()
        staged = staged.resolve()
        manifest = _manifest()
        hf_like = tmp_path / "models--Qwen--Qwen2.5-0.5B-Instruct" / "snapshots" / \
            CANDIDATE_MODEL.resolved_commit
        hf_like.mkdir(parents=True)
        wrong = _assignment(staged, manifest, artifact_cache_root=str(hf_like))
        with pytest.raises(WorkAuthorizationError, match="work_assignment_invalid"):
            _consent(tmp_path, assignment=wrong,
                     expected_assignment_digest=_digest(wrong))

    def test_assignment_runtime_tamper_fails_closed(self, tmp_path):
        staged = (tmp_path / "staged")
        staged.mkdir()
        staged = staged.resolve()
        manifest = _manifest()
        wrong = _assignment(staged, manifest,
                            runtime={"backend": "mlx", "dtype": "float16",
                                     "quantization": "int8-weight-only"})
        with pytest.raises(WorkAuthorizationError, match="work_assignment_invalid"):
            _consent(tmp_path, assignment=wrong,
                     expected_assignment_digest=_digest(wrong))

    def test_assignment_route_ready_true_fails_closed(self, tmp_path):
        staged = (tmp_path / "staged")
        staged.mkdir()
        staged = staged.resolve()
        manifest = _manifest()
        premature = _assignment(staged, manifest)
        premature["route_ready"] = True
        with pytest.raises(WorkAuthorizationError, match="work_assignment_invalid"):
            _consent(tmp_path, assignment=premature,
                     expected_assignment_digest=_digest(premature))

    def test_manifest_tamper_fails_closed(self, tmp_path):
        staged = (tmp_path / "staged")
        staged.mkdir()
        staged = staged.resolve()
        manifest = _manifest()
        assignment = _assignment(staged, manifest)
        tampered = json.loads(json.dumps(manifest))
        tampered["model_id"] = "other/model"
        with pytest.raises(WorkAuthorizationError, match="work_manifest_invalid"):
            _consent(tmp_path, manifest=tampered,
                     expected_assignment_digest=_digest(assignment))


class TestAuthorizeLoadMetadata:
    """Fake/metadata-only envelope evaluation: the exact-grant callable."""

    @pytest.fixture
    def consent(self, tmp_path):
        return _consent(tmp_path)

    def test_admits_consent_pinned_configure_shapes(self, consent):
        authz, _, manifest, _assignment = consent
        digest = mm.manifest_digest_ref(manifest)
        legacy = _configure_payload(digest)
        stage = {"assignment_file": "assignment.json", "manifest_file": "manifest.json",
                 "stage_pack_file": "stage.pack", "graph": _graph(digest),
                 "device_states": {}, "load_generation": 2}
        pixel = {"assignment_file": "assignment.json",
                 "pixel_stage_pack_file": "pixel.pack", "graph": _graph(digest),
                 "device_states": {}, "load_generation": 1}
        for payload in (legacy, stage, pixel):
            assert authz.authorize_load(_envelope("configure", payload)) is True

    def test_admits_start_infer_rotate_shapes(self, consent):
        authz, _, _manifest_value, _assignment = consent
        assert authz.authorize_load(_envelope(
            "start", {"peer": _peer("mac-other"), "local_generation": 1})) is True
        assert authz.authorize_load(_envelope(
            "start", {"peer": _peer("mac-other"), "peers": [_peer("mac-a"), _peer("mac-b")],
                      "local_generation": 1})) is True
        assert authz.authorize_load(_envelope(
            "infer_start", {"request": {"prompt": "hi"}})) is True
        assert authz.authorize_load(_envelope(
            "infer_start", {"request": {"prompt": "hi"},
                            "excluded_placement_ids": []})) is True
        assert authz.authorize_load(_envelope(
            "infer_start", {"request": {"prompt": "hi"}, "control": {},
                            "path_manifest": {}})) is True
        assert authz.authorize_load(_envelope(
            "infer_decode", {"request_id": "req-1", "count": 4})) is True
        assert authz.authorize_load(_envelope(
            "infer_decode", {"request_id": "req-1", "count": 1, "control": {}})) is True
        assert authz.authorize_load(_envelope("rotate", {"peer": _peer("mac-a")})) is True

    @pytest.mark.parametrize("envelope", [
        ["not", "a", "mapping"],
        _envelope("rotate", {"peer": _peer("mac-a")}, protocol="other.v1"),
        _envelope("rotate", {"peer": _peer("mac-a")}, run_id="run-other"),
        _envelope("rotate", {"peer": _peer("mac-a")}, deployment_id="99999999-2222-3333-4444-555555555555"),
        _envelope("rotate", {"peer": _peer("mac-a")}, command_id="bad id!"),
        _envelope("rotate", {"peer": _peer("mac-a")}, extra="field"),
        {k: v for k, v in _envelope("rotate", {"peer": _peer("mac-a")}).items()
         if k != "payload"},
        _envelope("hello", {}),
        _envelope("stop", {}),
        _envelope("exec", {"argv": ["touch", "no"]}),
        _envelope("infer_cancel", {"request_id": "r"}),
        _envelope("configure", {}),
        _envelope("configure", {"assignment_file": "assignment.json",
                                "artifact_report_file": "report.json",
                                "graph": _graph("sha256:" + "c" * 64),
                                "device_states": {}, "load_generation": 1,
                                "unexpected": True}),
    ])
    def test_denies_malformed_or_ungranted_envelopes(self, consent, envelope):
        authz, _, _, _assignment = consent
        assert authz.authorize_load(envelope) is False

    @pytest.mark.parametrize("graph_change", [
        {"model_id": "Qwen/Qwen2.5-0.5B-Instruct-tampered"},
        {"resolved_commit": "f" * 40},
        {"manifest_digest": "sha256:" + "c" * 64},
        {"deployment_id": "99999999-2222-3333-4444-555555555555"},
        {"protocol": "mycelium.execution_graph.v2"},
    ])
    def test_denies_configure_with_tampered_graph_pins(self, consent, graph_change):
        authz, _, manifest, _assignment = consent
        payload = _configure_payload(mm.manifest_digest_ref(manifest))
        payload["graph"] = {**payload["graph"], **graph_change}
        assert authz.authorize_load(_envelope("configure", payload)) is False

    def test_denies_configure_with_non_mapping_graph(self, consent):
        authz, _, manifest, _assignment = consent
        payload = _configure_payload(mm.manifest_digest_ref(manifest))
        payload["graph"] = "not-a-mapping"
        assert authz.authorize_load(_envelope("configure", payload)) is False

    @pytest.mark.parametrize("value", [
        "/etc/passwd", "../escape.json", "a/../b.json", "a/./b.json",
        "/".join(["d"] * 17) + ".json", 5, None, "",
    ])
    def test_denies_configure_files_outside_staged_root(self, consent, value):
        authz, _, manifest, _assignment = consent
        payload = _configure_payload(mm.manifest_digest_ref(manifest))
        payload["assignment_file"] = value
        assert authz.authorize_load(_envelope("configure", payload)) is False

    def test_admits_configure_files_inside_staged_root(self, consent):
        authz, _, manifest, _assignment = consent
        payload = _configure_payload(mm.manifest_digest_ref(manifest))
        payload["assignment_file"] = "deployment/assignment.json"
        assert authz.authorize_load(_envelope("configure", payload)) is True

    @pytest.mark.parametrize("change", [
        {"load_generation": 0}, {"load_generation": True},
        {"load_generation": "1"}, {"device_states": []},
    ])
    def test_denies_configure_with_invalid_load_state(self, consent, change):
        authz, _, manifest, _assignment = consent
        payload = _configure_payload(mm.manifest_digest_ref(manifest))
        payload.update(change)
        assert authz.authorize_load(_envelope("configure", payload)) is False

    @pytest.mark.parametrize("payload", [
        {"peer": "", "local_generation": 1},
        {"peer": "../peer", "local_generation": 1},
        {"peer": _peer("mac-a"), "local_generation": 0},
        {"peer": _peer("mac-a"), "local_generation": True},
        {"peer": _peer("mac-a"), "local_generation": "1"},
        {"peer": _peer("mac-a"), "peers": "mac-b", "local_generation": 1},
        {"peer": _peer("mac-a"), "peers": ["ok", 7], "local_generation": 1},
    ])
    def test_denies_invalid_start_payloads(self, consent, payload):
        authz, _, _, _assignment = consent
        assert authz.authorize_load(_envelope("start", payload)) is False

    @pytest.mark.parametrize("payload", [
        {}, {"request": None}, {"request": ""},
        {"request": {"prompt": "hi"}, "excluded_placement_ids": "x"},
        {"request": {"prompt": "hi"}, "control": []},
        {"request": {"prompt": "hi"}, "path_manifest": 5},
    ])
    def test_denies_invalid_infer_start_payloads(self, consent, payload):
        authz, _, _, _assignment = consent
        assert authz.authorize_load(_envelope("infer_start", payload)) is False

    @pytest.mark.parametrize("payload", [
        {"request_id": "r", "count": 0},
        {"request_id": "r", "count": True},
        {"request_id": "r", "count": "4"},
        {"request_id": "", "count": 4},
        {"request_id": 7, "count": 4},
        {"request_id": "r", "count": 4, "control": []},
    ])
    def test_denies_invalid_infer_decode_payloads(self, consent, payload):
        authz, _, _, _assignment = consent
        assert authz.authorize_load(_envelope("infer_decode", payload)) is False

    def test_denies_invalid_rotate_payloads(self, consent):
        authz, _, _, _assignment = consent
        assert authz.authorize_load(_envelope("rotate", {})) is False
        assert authz.authorize_load(_envelope("rotate", {"peer": 5})) is False


class TestBoundaryAlignment:
    """Metadata alignment with the authoritative worker boundary constants."""

    def test_gate_matches_worker_exactly(self):
        from mycelium_a13_macos import worker
        from mycelium_a13_macos import work_authorization
        assert work_authorization._NATIVE_PROTOCOL == worker._NATIVE_PROTOCOL
        assert (work_authorization._EXACT_GRANT_COMMANDS
                == worker._EXACT_GRANT_COMMANDS)
        for command in work_authorization._EXACT_GRANT_COMMANDS:
            assert (work_authorization._OPERATION_PAYLOAD_FIELDS[command]
                    == worker._OPERATION_PAYLOAD_FIELDS[command])


class TestRealNativeBoundary:
    """Physical proof with the REAL native node process; no model bytes load.

    The consent gate admits only a fully pinned configure, which then reaches
    the real native authoritative validation and is rejected there. A tampered
    pin is denied locally before any native call.
    """

    def _authorization(self, tmp_path):
        staged = tmp_path / "artifacts"
        staged.mkdir(exist_ok=True)
        staged = staged.resolve()
        manifest = _manifest()
        assignment = _assignment(staged, manifest)
        (staged/'control').mkdir(exist_ok=True)
        (staged/'control/execution-graph.json').write_text(json.dumps(_graph(mm.manifest_digest_ref(manifest))))
        authz = authorize_work_consent(
            offer=_offer(), acknowledged=True, limits=_limits(),
            staged_root=staged, assignment=assignment,
            expected_assignment_digest=_digest(assignment), manifest=manifest)
        return authz, manifest, assignment

    def _worker(self, tmp_path, authz):
        from test_worker import POLICY, SAFE
        from mycelium_a13_macos.worker import ManagedWorker
        return ManagedWorker(
            node_id=NODE_ID, run_id=RUN_ID, deployment_id=DEPLOYMENT_ID,
            artifact_root=Path(authz.pinned()["staged_root"]),
            socket_root=tmp_path / "socket", sidecar_binary=tmp_path / "not-started",
            resource_policy=POLICY, limits=authz.limits,
            membership_current=lambda: True,
            authorize_load=authz.authorize_load,
            _test_observer=lambda: dict(SAFE))

    def test_admitted_configure_reaches_real_native_validation(self, tmp_path):
        from mycelium_a13_macos.worker import WorkerError
        authz, manifest, assignment = self._authorization(tmp_path)
        staged = Path(authz.pinned()["staged_root"])
        (staged / "assignment.json").write_text(
            json.dumps(assignment, sort_keys=True, separators=(",", ":")),
            encoding="utf-8")
        (staged / "report.json").write_text("{}", encoding="utf-8")
        w = self._worker(tmp_path, authz)
        try:
            hello = w.start()
            assert hello["ok"] is True
            # Consent gate admits this fully pinned configure; the REAL native
            # node then applies its own authoritative validation and rejects
            # the metadata-only graph — a native-authored error response.
            reply = w.send(command_id="configure-admitted", command="configure",
                           payload=_configure_payload(mm.manifest_digest_ref(manifest)))
            assert reply["ok"] is False
            assert isinstance(reply["error"], dict)
            assert isinstance(reply["error"]["code"], str)
            assert reply["error"]["code"]
            assert w.status()["model_load_authorized"] is False
            with pytest.raises(WorkerError, match="model_load_not_admitted"):
                w.send(command_id="decode-blocked", command="infer_decode",
                       payload={"request_id": "r", "count": 1})
        finally:
            w.close()
        assert w.process_group_present() is False

    def test_denied_configure_never_reaches_native(self, tmp_path):
        from mycelium_a13_macos.worker import WorkerError
        authz, manifest, assignment = self._authorization(tmp_path)
        w = self._worker(tmp_path, authz)
        try:
            assert w.start()["ok"] is True
            payload = _configure_payload(mm.manifest_digest_ref(manifest))
            payload["graph"] = {**payload["graph"],
                                "model_id": "Qwen/Qwen2.5-0.5B-Instruct-tampered"}
            with pytest.raises(WorkerError, match="model_execution_not_authorized"):
                w.send(command_id="configure-denied", command="configure",
                       payload=payload)
            # The denial was local: the real native session stays healthy.
            assert w.status()["running"] is True
            followup = w.send(command_id="hello-after", command="hello", payload={})
            assert followup["ok"] is True
            assert w.status()["model_load_authorized"] is False
        finally:
            w.close()
        assert w.process_group_present() is False

    def test_real_hello_stop_unaffected_by_authorizer(self, tmp_path):
        authz, manifest, assignment = self._authorization(tmp_path)
        w = self._worker(tmp_path, authz)
        try:
            hello = w.start()
            assert hello["ok"] is True
            assert hello["route_ready"] is False
            assert w.status()["running"] is True
            assert w.status()["model_load_authorized"] is False
        finally:
            w.close()
        assert w.status()["running"] is False
        assert w.process_group_present() is False


@pytest.mark.parametrize('case',['scalar','self','outside-graph','address-mismatch','wrong-local-generation','duplicate'])
def test_start_peer_binding_rejects_unapproved_shape_or_scope(tmp_path,case):
    grant,*_=_consent(tmp_path)
    payload={'peer':_peer(),'local_generation':1}
    if case=='scalar':payload['peer']='mac-other'
    elif case=='self':payload['peer']=_peer(NODE_ID)
    elif case=='outside-graph':payload['peer']=_peer('unapproved-node')
    elif case=='address-mismatch':payload['peer']['endpoint_addr']={'id':'wrong'}
    elif case=='wrong-local-generation':payload['local_generation']=2
    else:payload['peers']=[_peer()]
    assert grant.authorize_load(_envelope('start',payload)) is False
