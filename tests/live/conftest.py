# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

import pytest
import functools
import weakref

from mycelium_qualification.evidence import sha256_bytes
from mycelium_qualification.qualifier import qualify_route
from mycelium_router.contracts import RequestContext
from mycelium_router.serialization import execution_graph_from_dict
from physical_inference_node import execution_graph_from_document


ROOT = Path(__file__).resolve().parents[2]
PLAN = Path(
    os.path.expanduser("~/.hermes/missions/mycelium-distributed-inference-mvp"
    "/evidence/g4-live/w8-mvp-live-533d107-20260809t091035z/operator-plan.json")
)
DEPLOYMENT = Path(os.path.expanduser("~/mycelium-mvp-stage-node0-533d107b/deployment"))


class RecordingSink:
    def __init__(self) -> None:
        self.tokens: list[tuple[int, int]] = []

    def emit(self, token_index: int, token_id: int) -> None:
        self.tokens.append((token_index, token_id))


@pytest.fixture(scope="session")
def deployment_dir() -> Path:
    if not (DEPLOYMENT / "vocab.json").exists():
        pytest.skip("staged deployment absent")
    return DEPLOYMENT


@pytest.fixture(scope="session")
def live_graph():
    if not PLAN.exists():
        pytest.skip("operator plan absent")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    node = plan["controller"]["run_plan"]["nodes"][0]
    return execution_graph_from_document(node["configure"]["graph"])


@pytest.fixture
def request_factory():
    def make_request(request_id: str) -> RequestContext:
        return RequestContext(
            request_id=request_id,
            prompt_token_ids=(15496, 11),
            max_new_tokens=3,
            expected_new_tokens=3,
            qos_class="interactive",
            admitted_at=0.0,
            target_ttft_ms=120000.0,
            target_tpot_ms=120000.0,
            target_tokens_per_second=0.001,
            sampling_seed=17,
            generation_config_digest="sha256:" + "0" * 64,
        )

    return make_request


@pytest.fixture
def recording_sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture(scope="session")
def qualified_route():
    spec = importlib.util.spec_from_file_location(
        "live_m0_qualification_fixture",
        ROOT / "tests" / "qualification" / "conftest.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    case = module.make_case()
    files, manifest = case.render()

    def verify(statement: bytes, signature: dict[str, Any]) -> bool:
        return (
            signature.get("algorithm") == "ed25519"
            and signature.get("signature")
            == "synthetic-test-signature-never-production"
            and signature.get("signed_statement_digest") == sha256_bytes(statement)
        )

    qualification = qualify_route(
        evidence_files=files,
        evidence_manifest=manifest,
        now_unix_ms=case.now_unix_ms,
        verify_gossip_signature=verify,
        verify_load_proof_signature=verify,
    )
    graph = execution_graph_from_dict(json.loads(files["router/execution-graph.json"]))
    return qualification, graph


@pytest.fixture(autouse=True, scope="module")
def _close_composed_live_routers():
    """Close every LiveRouterPort a test module composed and left open.

    An unclosed router keeps its m16-dispatch loop and live-route-worker pool
    running in the xdist worker, where they open descriptors at arbitrary
    moments and race process-wide fd-inventory tests in other modules (L2
    rehearsal of 3e761ed1: test_private_directory_walkers_keep_fd_inventory_
    stable_for_200_cycles). Module scope: tests in a module may share routers,
    and pytest tears module fixtures down before the worker runs another
    module's test. Product owners close their routers themselves (see
    test_deployment_registry and test_live_stack_close_router_stops_the_
    dispatch_thread); this only tears down routers composed directly by tests.
    """
    from mycelium_live import router_port

    created: "weakref.WeakSet[router_port.LiveRouterPort]" = weakref.WeakSet()
    original_init = router_port.LiveRouterPort.__init__

    # wraps(): tests read inspect.signature(LiveRouterPort.__init__) to detect
    # constructor parameters; the tracking wrapper must keep that signature.
    @functools.wraps(original_init)
    def tracking_init(self, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        created.add(self)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(router_port.LiveRouterPort, "__init__", tracking_init)
        yield
    for port in list(created):
        try:
            port.close()
        except RuntimeError as error:
            # close() joins the dispatcher and workers before it fails closed on
            # requests whose cleanup was never proven or whose cancellation a test
            # made fail on purpose; the threads are stopped either way. Shutdown
            # timeouts mean threads still run, and are real failures.
            if str(error) not in (
                "router_port_shutdown_cleanup_unproven",
                "router_port_shutdown_cancellation_failed",
            ):
                raise
