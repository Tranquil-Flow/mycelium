"""PV9: the two service modes as policy objects, bounded by what devices have proven.

``interactive`` is today's behaviour (a 2 ms collection window, the shipped batch cap) and asks for
speculation where a draft is staged.  ``batch`` is the throughput mode: a longer collection window
and queued jobs, but never a knob above the value an A10 device round proved; a knob with no proven
upper bound keeps today's value and says so.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mycelium_router import service_modes as sm
from mycelium_router.contracts import RouterConfig
from physical_inference_node import (
    A10_MAXIMUM_COLLECTION_WINDOW_MS,
    PhysicalNodeService,
    parse_a10_batch_policy,
)


def test_two_modes_and_fail_closed_parse():
    assert sm.MODES == ("interactive", "batch")
    assert sm.DEFAULT_MODE == "interactive"
    for bad in (None, "", "Batch", "overnight", 1, ["batch"], "interactive "):
        with pytest.raises(ValueError, match="service_mode_invalid"):
            sm.knobs_for(bad)


def test_interactive_is_exactly_todays_router_defaults():
    knobs = sm.knobs_for("interactive")
    today = RouterConfig()
    assert knobs.live_hop_collection_window_ms == today.live_hop_collection_window_seconds * 1000 == 2
    assert knobs.maximum_runtime_batch_size == today.maximum_runtime_batch_size
    assert knobs.decode_runtime_batch_size == today.decode_runtime_batch_size
    assert knobs.prefill_runtime_batch_size == today.prefill_runtime_batch_size
    assert knobs.maximum_runtime_batch_bytes == today.maximum_runtime_batch_bytes
    assert knobs.prefill_collection_window_ms == today.prefill_collection_window_seconds * 1000
    # interactive config is a no-op on a default node config
    assert sm.router_config_for_mode(today, "interactive") == today
    assert knobs.speculation_preference == "speculative_requested"
    assert knobs.accepts_queued_batch_jobs is False
    assert knobs.batch_jobs_need_sharing_allowed is False  # nothing queued to gate


def test_batch_raises_the_window_to_the_proven_value_and_nothing_above_any_proven_limit():
    knobs = sm.knobs_for("batch")
    interactive = sm.knobs_for("interactive")
    assert knobs.live_hop_collection_window_ms > interactive.live_hop_collection_window_ms
    for name, limit in sm.PROVEN_LIMITS.items():
        assert getattr(knobs, name) <= limit, name
    assert knobs.live_hop_collection_window_ms == sm.PROVEN_LIMITS["live_hop_collection_window_ms"] == 46
    assert knobs.accepts_queued_batch_jobs is True
    assert knobs.batch_jobs_need_sharing_allowed is True
    assert knobs.speculation_preference == "target_only"


def test_knobs_without_a_proven_upper_bound_keep_todays_value_and_are_recorded():
    batch = sm.knobs_for("batch")
    interactive = sm.knobs_for("interactive")
    assert set(sm.KEPT_AT_TODAY) >= {
        "maximum_runtime_batch_size",
        "decode_runtime_batch_size",
        "prefill_runtime_batch_size",
        "maximum_runtime_batch_bytes",
        "prefill_collection_window_ms",
        "prefill_chunk_size_tokens",
        "batch_job_concurrency",
    }
    for name in sm.KEPT_AT_TODAY:
        if name == "prefill_chunk_size_tokens":
            assert batch.prefill_chunk_size_tokens is None  # the node picks it from its decode mode
            continue
        assert getattr(batch, name) == getattr(interactive, name), name
    assert batch.batch_job_concurrency == 1
    assert set(sm.KEPT_AT_TODAY).isdisjoint({"live_hop_collection_window_ms"})


def test_every_knob_the_mode_changes_has_a_recorded_proof_source():
    for name in ("live_hop_collection_window_ms",):
        assert sm.PROVEN_LIMITS[name] <= A10_MAXIMUM_COLLECTION_WINDOW_MS
        assert sm.PROOF_SOURCES[name]
    assert set(sm.PROVEN_LIMITS) <= set(sm.PROOF_SOURCES)


def test_router_config_overlay_changes_only_the_window_for_batch():
    base = RouterConfig(prefill_chunk_size_tokens=8)
    config = sm.router_config_for_mode(base, "batch")
    assert config == replace(base, live_hop_collection_window_seconds=0.046)
    assert config.prefill_chunk_size_tokens == 8  # node-owned, never overridden
    # a node that was configured with a smaller cap is never raised above it
    small = RouterConfig(maximum_runtime_batch_size=4)
    assert sm.router_config_for_mode(small, "batch").maximum_runtime_batch_size == 4
    # and a base above the proven limit is clamped down
    wild = RouterConfig(live_hop_collection_window_seconds=0.002)
    assert sm.router_config_for_mode(wild, "batch").live_hop_collection_window_seconds == 0.046
    assert sm.router_config_for_mode(replace(base, maximum_runtime_batch_size=64), "batch").maximum_runtime_batch_size == 20


def test_wire_policy_is_accepted_by_the_nodes_own_parser_and_bound():
    assert sm.wire_policy_for_mode("interactive") == "candidate@2"
    assert sm.wire_policy_for_mode("batch") == "candidate@46"
    assert parse_a10_batch_policy(sm.wire_policy_for_mode("batch")) == ("candidate", 46)
    assert parse_a10_batch_policy(sm.wire_policy_for_mode("interactive")) == ("candidate", 2)


def test_describe_gives_one_plain_sentence_and_the_effective_knobs():
    for mode in sm.MODES:
        document = sm.describe(mode)
        assert document["mode"] == mode
        assert document["label"] in {"Interactive", "Batch / overnight"}
        sentence = document["sentence"]
        assert sentence.count(". ") == 0 and sentence.endswith(".") and len(sentence) <= 220
        assert set(document["knobs"]) >= {
            "live_hop_collection_window_ms",
            "maximum_runtime_batch_size",
            "speculation_preference",
            "accepts_queued_batch_jobs",
            "batch_job_concurrency",
        }
        assert document["kept_at_today"] == list(sm.KEPT_AT_TODAY)
        assert document["proven_limits"] == dict(sm.PROVEN_LIMITS)
    assert "2 ms" in sm.describe("interactive")["sentence"]
    assert "46 ms" in sm.describe("batch")["sentence"]
    json.dumps(sm.describe("batch"))


def test_mode_file_roundtrip_is_private_and_fails_closed_to_interactive(tmp_path):
    root = tmp_path / "cap"
    root.mkdir(mode=0o700)
    assert sm.read_service_mode(root) == "interactive"  # no file
    sm.write_service_mode(root, "batch")
    path = root / sm.SERVICE_MODE_FILENAME
    assert path.stat().st_mode & 0o777 == 0o600
    assert sm.read_service_mode(root) == "batch"
    sm.write_service_mode(root, "interactive")
    assert sm.read_service_mode(root) == "interactive"
    with pytest.raises(ValueError, match="service_mode_invalid"):
        sm.write_service_mode(root, "turbo")
    for raw in (b"{", b'{"schema":1,"mode":"turbo"}', b'{"schema":2,"mode":"batch"}',
                b'{"schema":1,"mode":"batch","x":1}', b"x" * 5000):
        path.unlink()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, raw)
        os.close(fd)
        assert sm.read_service_mode(root) == "interactive"
    path.unlink()
    path.write_text('{"schema":1,"mode":"batch"}')
    path.chmod(0o644)  # loose permission: not trusted
    assert sm.read_service_mode(root) == "interactive"
    path.unlink()
    target = tmp_path / "elsewhere"
    target.write_text('{"schema":1,"mode":"batch"}')
    target.chmod(0o600)
    path.symlink_to(target)
    assert sm.read_service_mode(root) == "interactive"


def _service(root, *, config=None, a10_override=False):
    from mycelium_router.fakes import ManualClock
    from mycelium_router.relay import RelayEngine
    from mycelium_router.scheduler import HopScheduler

    relay = RelayEngine(
        node_id="node-a", runtime=object(), transport=None,
        scheduler=HopScheduler(RouterConfig()), clock=ManualClock(), builder=None, device_states=None,
    )
    service = object.__new__(PhysicalNodeService)
    service.state = "RUNNING"
    service.router = SimpleNamespace(relay=relay)
    service._router_config = config or RouterConfig()
    service._control_lock = threading.RLock()
    service.capacity_root = root
    if a10_override:
        service._a10_batch_policy_applied = True
    return service, relay


def test_node_applies_the_mode_file_to_the_relay_before_the_next_request(tmp_path):
    root = tmp_path / "cap"
    root.mkdir(mode=0o700)
    service, relay = _service(root)
    assert service._refresh_service_mode() == "interactive"
    assert relay.batch_controller.config.live_hop_collection_window_seconds == 0.002
    sm.write_service_mode(root, "batch")
    assert service._refresh_service_mode() == "batch"
    assert relay.batch_scheduler.config.live_hop_collection_window_seconds == 0.046
    assert relay.batch_controller.config.live_hop_collection_window_seconds == 0.046
    assert service._router_config.live_hop_collection_window_seconds == 0.002  # base config untouched
    sm.write_service_mode(root, "interactive")
    service._refresh_service_mode()
    assert relay.batch_controller.config.live_hop_collection_window_seconds == 0.002


def test_node_does_not_override_an_a10_benchmark_policy(tmp_path):
    root = tmp_path / "cap"
    root.mkdir(mode=0o700)
    service, relay = _service(root, a10_override=True)
    sm.write_service_mode(root, "batch")
    before = relay.batch_controller.config
    assert service._refresh_service_mode() is None
    assert relay.batch_controller.config is before


def test_node_without_a_running_router_ignores_the_file(tmp_path):
    root = tmp_path / "cap"
    root.mkdir(mode=0o700)
    service, _ = _service(root)
    service.router = None
    sm.write_service_mode(root, "batch")
    assert service._refresh_service_mode() is None
