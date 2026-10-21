# SPDX-License-Identifier: AGPL-3.0-only
"""Fast deterministic checks for A6 production composition only."""
from __future__ import annotations

import inspect

from pathlib import Path
from types import SimpleNamespace

from mycelium_live.router_port import LiveRouterPort
from mycelium_live import supervisor


def test_live_stack_wires_qualifier_owned_a6_authority(monkeypatch, tmp_path: Path) -> None:
    coordinator = object()
    built_app = object()
    captured_router: dict[str, object] = {}
    captured_stack: dict[str, object] = {}
    captured_product: dict[str, object] = {}

    class RouterProbe:
        def __init__(self, **kwargs: object) -> None:
            captured_router.update(kwargs)

        def is_idle(self) -> bool:
            return True

        def recovery_public_projection(self) -> dict[str, object]:
            return {"protocol": "mycelium.full_context_recovery_runtime.v1"}

    class RecoveryAuthorityProbe:
        def __init__(self, supplied: object) -> None:
            assert supplied is coordinator
            self.coordinator = supplied

    def build_stack_probe(**kwargs: object) -> object:
        captured_stack.update(kwargs)
        return built_app

    monkeypatch.setattr(
        supervisor,
        "prompt_codec_from_deployment",
        lambda _path: SimpleNamespace(stop_token_ids=frozenset()),
    )
    monkeypatch.setattr(supervisor, "_placement_projection", lambda _path: ())
    monkeypatch.setattr(supervisor, "_workload_comparison", lambda _path: ())
    monkeypatch.setattr(supervisor, "_m16_performance_budget", lambda _path: None)
    monkeypatch.setattr(
        supervisor,
        "build_live_m16_runtime",
        lambda *_args, **_kwargs: coordinator,
    )
    monkeypatch.setattr(supervisor, "LiveRouterPort", RouterProbe)
    monkeypatch.setattr(supervisor, "M16RecoveryAuthority", RecoveryAuthorityProbe)
    def product_probe(_route, _qualification_source, **kwargs: object) -> object:
        captured_product.update(kwargs)
        return object()

    monkeypatch.setattr(supervisor, "build_loopback_product_stack", build_stack_probe)
    monkeypatch.setattr(supervisor, "_optional_product_application", product_probe)
    monkeypatch.setattr(
        supervisor,
        "LiveSwarmCoordinator",
        lambda *_args, **_kwargs: object(),
    )

    route = SimpleNamespace()

    def qualifier(
        _request_id,
        _checkpoint,
        _successor_identity,
        _successor_manifest,
    ):
        return {}, {}

    state_root = tmp_path / "product-state"

    stack = supervisor.build_live_stack(
        route=route,
        deployment_dir=tmp_path / "deployment",
        execution_graph=SimpleNamespace(deployment_id="deployment-a6"),
        bearer_token="test-token",
        product_state_root=state_root,
        recovery_qualification_source=qualifier,
        recovery_tokenizer_digest="sha256:" + "a" * 64,
    )

    assert captured_router["runtime_coordinator"] is coordinator
    authority = captured_router["recovery_authority"]
    assert isinstance(authority, RecoveryAuthorityProbe)
    assert authority.coordinator is coordinator
    assert captured_router["recovery_qualification_source"] is qualifier
    assert captured_router["recovery_tokenizer_digest"] == "sha256:" + "a" * 64
    # The idle recovery projection and the request gateway must share one
    # qualifier-owned current-record source: the browser polls the projection
    # before the first admission, so the port cannot depend on admit alone.
    assert (
        captured_router.get("qualification_source")
        is captured_stack["qualification_source"]
    )
    factory = captured_stack["product_app_factory"]
    assert callable(factory)
    recovery_source = RouterProbe().recovery_public_projection
    factory(recovery_source)
    assert captured_product["recovery_source"] is recovery_source
    assert captured_stack["replay_checkpoint_root"] == (
        state_root / "private" / "a6-replay-checkpoints"
    )
    assert stack.app is built_app


def test_physical_product_stack_constructs_live_a6_qualifier(monkeypatch, tmp_path: Path) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "config.json").write_text('{"model_type":"qwen2"}\n')
    (deployment / "tokenizer.json").write_text('{"version":"1.0"}\n')
    captured: dict[str, object] = {}
    sentinel = object()

    def build_probe(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(supervisor, "build_live_stack", build_probe)
    route = SimpleNamespace(startup_challenge=((1,), (2,)))
    graph = SimpleNamespace(
        deployment_id="deployment-a6",
        deployment_epoch=1,
        topology_version=1,
        model_id="model-a6",
        resolved_commit="immutable-revision",
        manifest_digest="sha256:" + "a" * 64,
    )
    result = supervisor._build_physical_product_stack(
        route=route,
        deployment_dir=deployment,
        execution_graph=graph,
        bearer_token="bounded-test-token",
        product_state_root=tmp_path / "state",
        seed_url=None,
        public_origin="https://a8.example.test",
        trusted_https_proxy=True,
        trusted_proxy_capability=b"a" * 64,
    )

    assert captured["public_origin"] == "https://a8.example.test"
    assert captured["trusted_https_proxy"] is True
    assert captured["trusted_proxy_capability"] == b"a" * 64
    assert result is sentinel
    assert captured["route"] is route
    assert captured["execution_graph"] is graph
    assert captured["recovery_qualification_source"].__class__.__name__ == (
        "LiveA6RecoveryQualifier"
    )
    digest = captured["recovery_tokenizer_digest"]
    assert isinstance(digest, str) and digest.startswith("sha256:")
    assert captured["recovery_a4_atomic_commit"] == (
        "599e3936db48d8b167a65e53dcdc94db2b4bc20c"
    )


def test_optional_product_application_retains_gateway_recovery_source(monkeypatch) -> None:
    from tests.product_spine.test_a6_recovery_projection import RAW_RECOVERY

    source = lambda: RAW_RECOVERY
    route = SimpleNamespace(
        product_membership_records=lambda: [],
        product_pseudonym_salt=lambda: b"a" * 32,
        public_status=lambda: None,
    )
    app = supervisor._optional_product_application(
        route, SimpleNamespace(current_qualification=lambda: None),
        recovery_source=source,
    )
    assert app is not None
    assert app._recovery_source is source


def test_live_recovery_does_not_swallow_process_level_exceptions() -> None:
    source = inspect.getsource(LiveRouterPort.recover_full_context)
    assert "except BaseException" not in source
    assert "except Exception" in source
