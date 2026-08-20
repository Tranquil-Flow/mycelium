from __future__ import annotations

import importlib.util
from pathlib import Path
import threading
import time


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "run_a4_product_gate",
    ROOT / "scripts" / "run_a4_product_gate.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _runtime(*, active: list[str], terminal: bool) -> dict:
    requests = []
    if terminal:
        requests = [
            {"request_id": "request-a", "terminal_state": "cancelled"},
            {"request_id": "request-b", "terminal_state": "completed"},
        ]
    return {
        "deployment_id": "deployment-a4",
        "deployment_epoch": 4,
        "topology_version": 7,
        "graph_digest": "sha256:" + "a" * 64,
        "queue": {
            "depth": 0,
            "active_request_ids": active,
            "maximum_active_requests": 4,
        },
        "placements": [
            {
                "placement_id": "placement-a",
                "active_reservations": len(active),
            }
        ],
        "requests": requests,
        "batch_state": {
            "mode": "sequential_dispatch",
            "continuous_batching": False,
            "pipeline_overlap": False,
        },
    }


def test_product_gate_is_privacy_reduced_and_requires_real_overlap(monkeypatch) -> None:
    cancelled = threading.Event()
    session_index = iter((0, 1))

    class Session:
        def __init__(self, _base_url):
            self.index = next(session_index)
            self._qualification = {
                "binding": {
                    "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                    "resolved_commit": "a" * 40,
                    "manifest_digest": "sha256:" + "b" * 64,
                    "qualification_digest": "sha256:" + "c" * 64,
                    "path_manifest_digest": "sha256:" + "d" * 64,
                }
            }

        def submit(self, *, label, maximum_new_tokens):
            assert maximum_new_tokens == 32
            return {
                "request_id": f"request-{'a' if self.index == 0 else 'b'}",
                "event_path": f"/events/{self.index}",
                "cancel_path": f"/cancel/{self.index}",
            }

        def cross_session_stream_denied(self, event_path):
            return event_path == "/events/0"

        def cancel(self, accepted):
            assert accepted["request_id"] == "request-a"
            cancelled.set()
            return {"status": "cancelling"}

        def stream_summary(self, accepted):
            if accepted["request_id"] == "request-a":
                assert cancelled.wait(timeout=1.0)
                terminal = "cancelled"
            else:
                terminal = "completed"
            return {
                "request_id": accepted["request_id"],
                "event_counts": {terminal: 1},
                "publisher_generation": 1,
                "first_sequence": 0,
                "last_sequence": 1,
                "terminal": terminal,
                "terminal_at_monotonic_s": time.monotonic(),
            }

    runtime_calls = 0

    def public_json(_base_url, path):
        nonlocal runtime_calls
        if path == "/__mycelium/live-status":
            return {
                "route_alive": True,
                "counters": {
                    "frames_sent": runtime_calls,
                    "frames_received": runtime_calls,
                    "applied_operation_count": runtime_calls,
                    "fatal": None,
                },
            }
        runtime_calls += 1
        if runtime_calls == 1:
            return _runtime(active=[], terminal=False)
        if runtime_calls == 2:
            return _runtime(active=["request-a", "request-b"], terminal=False)
        return _runtime(active=[], terminal=True)

    monkeypatch.setattr(MODULE, "ProductSession", Session)
    monkeypatch.setattr(MODULE, "public_json", public_json)
    report = MODULE.run_gate("http://product.invalid", maximum_new_tokens=32)

    assert report["qualification_claim"] is False
    assert report["promotion_authorized"] is False
    assert report["cancellation"]["within_total_bound"] is True
    assert report["cross_session_stream_denied"] is True
    assert report["final_queue"]["active_request_ids"] == []
    rendered = repr(report).lower()
    for forbidden in (
        "prompt",
        "decoded text",
        "csrf",
        "cookie",
        "hostname",
        "address",
        "command line",
    ):
        assert forbidden not in rendered
