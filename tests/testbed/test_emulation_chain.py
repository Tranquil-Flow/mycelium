# SPDX-License-Identifier: AGPL-3.0-only
"""Single-host two-worker C4->C5 emulation tests (PLAN 5.2 item 3).

These tests run the real thing: two worker *processes* with separate homes and
identities, the real ``Qwen2.5-0.5B-Instruct`` snapshot from the local HuggingFace
cache, a real ``https://`` origin on loopback, and the promoted seeded fault proxy
on every leg.  They are skipped only when the model is genuinely absent from disk -
this harness never downloads one.

The run writes its lifecycle traces into ``runs/wp2-4/`` (outside the repository,
PLAN 5.5) by default; override with ``MYCELIUM_TESTBED_RUNS_ROOT``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

from mycelium_deadlines import (
    CANCEL_AND_CLEANUP_BUDGET_SECONDS,
    PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS,
    advertised_maximum_total_cleanup_ms,
    route_cleanup_fanout_window_ms,
)
from mycelium_testbed import emulation as emulation_module
from mycelium_testbed.emulation import (
    CLEANUP_RECEIPT_TIMEOUT,
    MODEL_ID,
    MODEL_REVISION,
    NEGATIVE_TOKEN_STEPS,
    PROMPT,
    TOKEN_STEPS,
    WORKER_HOSTS,
    EmulationResult,
    local_model_root,
    model_present,
    run_emulation,
)
from mycelium_testbed.trace import (
    EventKind,
    LIFECYCLE_ORDER,
    load_run,
    merge_timeline,
)

DEFAULT_RUNS_ROOT = Path(
    os.path.expanduser("~/Documents/mycelium-core-program/runs/wp2-4")
)

REQUIRED_KINDS = {kind.value for kind in LIFECYCLE_ORDER}

#: The greedy completion two independent full runs produced on this host
#: (m4pro, CPython 3.14.4, the MLX build in the environment on 2026-09-15) for the
#: fixed ChatML prompt over the pinned snapshot.  Pinned on purpose: the decode is
#: greedy, so this is a golden value, and a drift should be a loud failure.
EMULATION_GOLDEN_TOKENS = (40, 2776, 14589, 11, 714, 358, 646, 944)


def _runs_root() -> Path:
    override = os.environ.get("MYCELIUM_TESTBED_RUNS_ROOT")
    root = Path(override) if override else DEFAULT_RUNS_ROOT
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pytest.skip(f"runs root not writable: {root}")
    return root


@pytest.fixture(scope="module")
def emulation_run() -> EmulationResult:
    model_root = local_model_root()
    if not model_present(model_root):
        pytest.skip(
            f"pilot model absent: expected {MODEL_ID} at revision {MODEL_REVISION} "
            f"under {model_root} (this harness never downloads)"
        )
    run_dir = _runs_root() / "acceptance"
    shutil.rmtree(run_dir, ignore_errors=True)
    return run_emulation(run_dir=run_dir, model_root=model_root)


@pytest.fixture(scope="module")
def emulation_document(emulation_run: EmulationResult) -> dict:
    return emulation_run.document


# --------------------------------------------------------------------------- #
# the successful terminal frame
# --------------------------------------------------------------------------- #


def test_the_request_reaches_a_successful_terminal_frame(
    emulation_run: EmulationResult,
) -> None:
    assert emulation_run.terminal_reached is True
    status = emulation_run.document["status"]
    assert status["ok_terminal"] == "completed"
    assert len(emulation_run.tokens) == TOKEN_STEPS
    assert all(isinstance(token, int) and token >= 0 for token in emulation_run.tokens)
    assert emulation_run.generated_text
    assert emulation_run.document["model"]["id"] == MODEL_ID
    assert emulation_run.document["model"]["revision"] == MODEL_REVISION


def test_tokens_streamed_over_real_tls_incrementally(
    emulation_run: EmulationResult,
) -> None:
    ok_request = emulation_run.document["trace"]["requests"][0]
    stream = emulation_run.document["streams"][ok_request]
    assert stream["status"] == 200
    assert stream["content_type"].startswith("text/event-stream")
    assert stream["x_accel_buffering"] == "no"
    assert stream["tls_version"].startswith("TLS")
    assert stream["peer_cert_subject"] and "localhost" in stream["peer_cert_subject"]

    events = stream["events"]
    names = [event["event"] for event in events]
    assert names[0] == "accepted"
    assert names[-1] == "done"
    assert names.count("token") == TOKEN_STEPS
    assert names.index("terminal") > names.index("token")

    token_times = [event["t_s"] for event in events if event["event"] == "token"]
    # a real stream, not one buffered blob: every token has its own arrival time
    assert len(set(token_times)) == len(token_times)
    assert token_times == sorted(token_times)
    terminal_time = next(
        event["t_s"] for event in events if event["event"] == "terminal"
    )
    assert terminal_time > token_times[-1]
    assert stream["elapsed_s"] > token_times[-1]


# --------------------------------------------------------------------------- #
# two worker processes, separate homes and identities
# --------------------------------------------------------------------------- #


def test_two_worker_processes_with_separate_homes_and_identities(
    emulation_document: dict,
) -> None:
    workers = emulation_document["workers"]
    assert set(workers) == set(WORKER_HOSTS)

    pids = {workers[node]["pid"] for node in WORKER_HOSTS}
    assert len(pids) == len(WORKER_HOSTS)
    assert all(pid > 0 for pid in pids)

    homes = {workers[node]["home"] for node in WORKER_HOSTS}
    assert len(homes) == len(WORKER_HOSTS)
    for node in WORKER_HOSTS:
        home = Path(workers[node]["home"])
        assert (home / "identity.json").is_file()
        assert (home / "identity.key").is_file()
        # the private key is owner-only
        assert (home / "identity.key").stat().st_mode & 0o777 == 0o600

    endpoint_ids = {workers[node]["identity"]["endpoint_id"] for node in WORKER_HOSTS}
    assert len(endpoint_ids) == len(WORKER_HOSTS)
    assert all(value.startswith("sha256:") for value in endpoint_ids)

    # separate homes => separate home digests
    digests = {node: emulation_document["home_digests"][node] for node in WORKER_HOSTS}
    assert len(set(digests.values())) == len(WORKER_HOSTS)


def test_the_layer_split_covers_the_whole_model_once(
    emulation_document: dict,
) -> None:
    workers = emulation_document["workers"]
    ranges = {node: tuple(workers[node]["stage"]["layers"]) for node in WORKER_HOSTS}
    assert ranges["worker-a"] == (0, 12)
    assert ranges["worker-c"] == (12, 24)
    assert ranges["worker-a"][1] == ranges["worker-c"][0]
    assert ranges["worker-a"][0] == 0 and ranges["worker-c"][1] == 24
    assert workers["worker-a"]["stage"]["owns_embed"] is True
    assert workers["worker-a"]["stage"]["owns_head"] is False
    assert workers["worker-c"]["stage"]["owns_embed"] is False
    assert workers["worker-c"]["stage"]["owns_head"] is True
    # Qwen2.5-0.5B ties its embeddings, so the last placement legitimately holds
    # the output projection too - and the stage says so rather than hiding it.
    assert workers["worker-c"]["stage"]["head_tied_to_embeddings"] is True


def test_the_real_model_is_read_from_the_local_snapshot(
    emulation_document: dict,
) -> None:
    model_root = Path(emulation_document["model"]["root"])
    assert model_root == local_model_root()
    assert (model_root / "model.safetensors").is_file()
    assert "no download" in emulation_document["model"]["source"]

    workers = emulation_document["workers"]
    stage_a = workers["worker-a"]["stage"]
    stage_c = workers["worker-c"]["stage"]
    assert stage_a["model_id"] == MODEL_ID and stage_c["model_id"] == MODEL_ID
    assert stage_a["config_digest"] == stage_c["config_digest"]
    assert stage_a["resident_bytes"] > 0 and stage_c["resident_bytes"] > 0
    # 12 layers x 12 tensors + the embedding (worker-a) / final norm (worker-c);
    # the tied embedding is loaded by both, so the unique total is one less.
    assert stage_a["tensors"] + stage_c["tensors"] - 1 == 290


def test_the_worker_memory_envelope_accounts_for_what_the_stage_loaded(
    emulation_document: dict,
) -> None:
    """The memory claim is the stage's own accounting, and that is deterministic.

    What is asserted is what each stage accounted for when it loaded its placement
    (the bytes of the tensors it checked in), cross-checked against the snapshot's
    own header - file metadata, read without this process's RSS anywhere in it.
    Peak RSS is *recorded* beside this, never asserted: it is an observation of the
    OS, and under memory pressure macOS reclaims and compresses pages, so a sampled
    peak may legitimately land below the bytes the stage loaded.
    """

    snapshot = emulation_document["model"]["safetensors"]
    assert snapshot["tensors"] == 290
    assert snapshot["payload_bytes"] > 0
    for node in WORKER_HOSTS:
        worker = emulation_document["workers"][node]
        memory = worker["memory"]
        assert memory["deterministic_violations"] == [], (node, memory)
        accounted = memory["accounted"]
        assert accounted["deterministic"] is True
        assert accounted["basis"] == emulation_module.STAGE_ACCOUNTING_BASIS
        assert accounted["resident_bytes"] == worker["stage"]["resident_bytes"] > 0, node
        assert accounted["tensors"] == worker["stage"]["tensors"] > 0, node
        # the declared bytes are exactly the sum of the tensors it checked in
        assert sum(accounted["bytes_by_dtype"].values()) == accounted["resident_bytes"]
        assert accounted["bytes_by_dtype"], node
        # and the snapshot's own header agrees, tensor for tensor
        expected = memory["expected_from_snapshot"]
        assert expected["resident_bytes"] == accounted["resident_bytes"], node
        assert expected["tensors"] == accounted["tensors"], node
        assert expected["source"].startswith("model.safetensors header")
        assert accounted["resident_bytes"] <= snapshot["payload_bytes"], node


def test_the_two_placements_account_for_every_snapshot_weight_once(
    emulation_document: dict,
) -> None:
    """Deterministic whole-model check: the split covers the checkpoint, in bytes."""

    snapshot = emulation_document["model"]["safetensors"]
    workers = emulation_document["workers"]
    accounted = {
        node: workers[node]["memory"]["accounted"] for node in WORKER_HOSTS
    }
    # the tied embedding is loaded by both placements and by nothing else twice
    assert workers["worker-a"]["stage"]["head_tied_to_embeddings"] is False
    assert workers["worker-c"]["stage"]["head_tied_to_embeddings"] is True
    unique_tensors = (
        accounted["worker-a"]["tensors"]
        + accounted["worker-c"]["tensors"]
        - 1
    )
    unique_bytes = (
        accounted["worker-a"]["resident_bytes"]
        + accounted["worker-c"]["resident_bytes"]
        - snapshot["tied_embedding_bytes"]
    )
    assert unique_tensors == snapshot["tensors"]
    assert unique_bytes == snapshot["payload_bytes"]


def test_the_worker_rss_is_recorded_as_an_observation_with_its_method(
    emulation_document: dict,
) -> None:
    """RSS is evidence, not a verdict: recorded, with how it was sampled."""

    for node in WORKER_HOSTS:
        observation = emulation_document["workers"][node]["memory"]["observation"]
        assert observation["quantity"] == "peak_resident_set_size_sampled", node
        assert observation["load_sensitive"] is True, node
        assert observation["samples"] > 0, node
        assert observation["peak_rss_bytes"] > 0, node
        assert observation["peak_rss_bytes"] == emulation_document["workers"][node][
            "peak_rss_bytes"
        ]
        assert observation["method"] == emulation_module.RSS_SAMPLING_METHOD
        assert "ps" in observation["method"] and "peak" in observation["method"]


def test_the_memory_verdict_is_invariant_under_a_sampling_shortfall(
    emulation_document: dict,
) -> None:
    """The gate's failure mode, reproduced as a test: loaded machine, green suite.

    The readiness gate ran ``39 passed`` when idle and failed this check while
    ``npm run test`` and other checks competed for the machine
    (``assert 553238528 >= (630166528 * 0.9)``).  A sampled shortfall must not be
    able to change the verdict, so this starves the sampler on purpose and requires
    the deterministic outcome to be identical.
    """

    for node in WORKER_HOSTS:
        worker = emulation_document["workers"][node]
        baseline = emulation_module.evaluate_worker_memory_envelope(worker)
        assert baseline.deterministic_violations == (), node
        starved = {
            **worker,
            # exactly the shape the loaded run showed: the sampler sees less than
            # the stage accounted for
            "peak_rss_bytes": baseline.accounted_resident_bytes // 2,
            "rss_samples": 1,
        }
        shortfall = emulation_module.evaluate_worker_memory_envelope(starved)
        assert shortfall.deterministic_violations == baseline.deterministic_violations
        assert shortfall.deterministic_ok is True
        assert shortfall.accounted_resident_bytes == baseline.accounted_resident_bytes
        assert shortfall.observed_peak_rss_bytes < shortfall.accounted_resident_bytes
        # the sampled bound is declared load-sensitive instead of deciding colour
        assert "rss_sanity_bound" in shortfall.load_sensitive_checks
        assert shortfall.reclaim_tolerance == pytest.approx(
            emulation_module.RSS_RECLAIM_TOLERANCE
        )


# --------------------------------------------------------------------------- #
# receipts and per-placement work counters
# --------------------------------------------------------------------------- #


def test_receipts_and_per_placement_work_counters(
    emulation_run: EmulationResult,
) -> None:
    assert set(emulation_run.receipts) == set(WORKER_HOSTS)
    for node, receipt in emulation_run.receipts.items():
        assert receipt["node_id"] == node
        assert receipt["receipt_digest"].startswith("sha256:")
        assert receipt["cleanup_ms"] >= 0.0
        assert receipt["kv_released"] is True

    assert len(emulation_run.placement_work) == len(WORKER_HOSTS)
    for entry in emulation_run.placement_work:
        assert entry["node_id"] in WORKER_HOSTS
        assert entry["layers"] in ([0, 12], [12, 24])
        assert entry["commands"] == TOKEN_STEPS + 1  # one prefill, then one per token
        assert entry["layers_executed"] == 12 * entry["commands"]
        assert entry["model_ms_total"] > 0.0

    by_node = {entry["node_id"]: entry for entry in emulation_run.placement_work}
    assert by_node["worker-a"]["hidden_bytes_out"] > 0
    assert by_node["worker-a"]["tokens_processed"] == 0
    assert by_node["worker-c"]["hidden_bytes_in"] > 0
    # prefill yields a token too, so worker-c produces one more than it streams
    assert by_node["worker-c"]["tokens_processed"] == TOKEN_STEPS + 1


# --------------------------------------------------------------------------- #
# the negative case: held receipt, named failure, never stuck
# --------------------------------------------------------------------------- #


def test_the_negative_case_ends_in_a_named_failure_within_budget(
    emulation_run: EmulationResult,
) -> None:
    assert emulation_run.negative_case_is_named_failure is True
    status = emulation_run.negative_terminal_status
    assert status.startswith(CLEANUP_RECEIPT_TIMEOUT)
    blockers = status.split("|", 1)[1]
    assert blockers
    assert "worker-c" in blockers
    assert emulation_run.negative_receipts_missing == ("worker-c",)

    document_status = emulation_run.document["status"]
    assert document_status["negative_within_budget"] is True
    cancellation_to_terminal = document_status["negative_cancellation_to_terminal_ms"]
    assert cancellation_to_terminal <= document_status["negative_budget_ms"]
    # it really waited out the route's fanout window rather than failing early
    assert (
        cancellation_to_terminal >= document_status["negative_fanout_window_ms"] - 100.0
    )
    assert document_status["negative_budget_ms"] == pytest.approx(
        CANCEL_AND_CLEANUP_BUDGET_SECONDS * 1000.0
    )


def test_the_negative_case_streamed_tokens_before_it_failed(
    emulation_run: EmulationResult,
) -> None:
    negative_request = emulation_run.document["trace"]["requests"][1]
    request = emulation_run.document["requests"][negative_request]
    assert request["phase"] == "negative"
    assert len(request["tokens"]) == NEGATIVE_TOKEN_STEPS
    assert request["terminal_status"].startswith(CLEANUP_RECEIPT_TIMEOUT)
    stream = emulation_run.document["streams"][negative_request]
    names = [event["event"] for event in stream["events"]]
    assert names.count("token") == NEGATIVE_TOKEN_STEPS
    # the terminal frame arrived and the stream closed: the request ended
    assert names[-1] == "done"
    terminal_event = next(
        event for event in stream["events"] if event["event"] == "terminal"
    )
    assert terminal_event["data"]["status"].startswith(CLEANUP_RECEIPT_TIMEOUT)


def test_the_receipt_hold_actually_fired_in_the_relay(
    emulation_document: dict,
) -> None:
    worker_c = emulation_document["relays"]["worker-c"]["stats"]
    assert worker_c["held"] >= 1
    # and it did not hold the token-carrying traffic of the successful request
    assert emulation_document["relays"]["worker-a"]["stats"]["held"] == 0
    assert (
        emulation_document["round50_shaping"][
            "receipt_hold_armed_ms_at_negative_cleanup"
        ]
        > route_cleanup_fanout_window_ms
    )


# --------------------------------------------------------------------------- #
# round-50 shaping, measured on this host
# --------------------------------------------------------------------------- #


def test_every_leg_was_shaped_by_the_seeded_fault_proxy(
    emulation_document: dict,
) -> None:
    relays = emulation_document["relays"]
    assert set(relays) == {"origin", "worker-a", "worker-c"}
    for name, relay in relays.items():
        assert relay["seed"] == emulation_document["seed"], name
        assert relay["stats"]["connections"] > 0, name
        assert relay["stats"]["trace_events"] > 0, name
        assert len(relay["stats"]["trace_digest"]) == 16, name
        assert relay["stats"]["delayed"] > 0, name
    # the shaped path is not the raw path: the proxy delayed every command's bytes
    assert relays["worker-a"]["stats"]["delay_ms_total"] > 0.0
    assert relays["worker-c"]["stats"]["delay_ms_total"] > 0.0


def test_the_route_receipt_grid_reproduces_the_research_sample(
    emulation_document: dict,
) -> None:
    grid = emulation_document["route_receipt_grid"]
    assert grid["trials"] >= 20
    assert grid["cadence_seconds"] == pytest.approx(0.1)
    # the measured observation delay is bounded by the cadence and centred in it
    assert 0.0 <= grid["min_ms"] <= 15.0
    assert grid["max_ms"] <= 101.0
    assert 25.0 <= grid["mean_ms"] <= 80.0
    research = grid["research_p3"]
    assert research["source"] == "research-mailbox-longpoll.md 4.3 phase P3"
    assert research["n"] == 200
    # honest about what n=40 cannot claim
    assert "cannot claim a p95" in grid["note"]


def test_the_round50_composition_now_holds_in_the_emulated_chain(
    emulation_document: dict,
) -> None:
    """Packet wp5-d9 closed the round-50 mismatch; a13-resolve fixed its value.

    The emulation's D9 verdict is the registry's own answer for the shipped
    composition: with the advertised bound derived as ``window - transport
    margin - slack`` it is the ok form, naming both numbers, never the violation
    reason - and it sits below the ceiling the route's own admission predicate
    applies (``window - margin``), so the shipped node cannot refuse itself.
    """

    shaping = emulation_document["round50_shaping"]
    assert shaping["d9_verdict"] == (
        f"ok:{advertised_maximum_total_cleanup_ms}<="
        f" {route_cleanup_fanout_window_ms}"
    )
    assert shaping["peer_advertised_cleanup_ms"] == (
        advertised_maximum_total_cleanup_ms
    )
    assert shaping["route_fanout_window_ms"] == route_cleanup_fanout_window_ms
    assert shaping["peer_advertised_cleanup_ms"] <= shaping["route_fanout_window_ms"]
    assert shaping["peer_advertised_cleanup_ms"] <= (
        shaping["route_fanout_window_ms"] - PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS
    )
    assert shaping["cancel_and_cleanup_budget_ms"] == pytest.approx(
        CANCEL_AND_CLEANUP_BUDGET_SECONDS * 1_000.0
    )
    assert shaping["measured"]
    assert shaping["unmeasured"]


# --------------------------------------------------------------------------- #
# lifecycle traces
# --------------------------------------------------------------------------- #


def test_the_lifecycle_trace_covers_both_requests_and_every_command(
    emulation_run: EmulationResult,
) -> None:
    run = load_run(emulation_run.run_dir)
    assert run.header.reference_host == "driver"
    assert len(run.requests) == 2
    assert len(run.commands) >= 2 * (TOKEN_STEPS + 1) + 2
    assert len(run.samples) == len(WORKER_HOSTS)

    kinds = {str(event.kind) for event in run.events}
    assert REQUIRED_KINDS <= kinds
    assert {event.host_id for event in run.events} == {"driver", *WORKER_HOSTS}
    assert all(event.recorder_id for event in run.events)
    assert all(event.pid > 0 for event in run.events)
    # every command event carries the placement it belongs to
    assert all(event.placement_id for event in run.events if event.command_id)

    entries = merge_timeline(run)
    assert entries
    assert list(entries) == sorted(entries, key=lambda entry: entry.monotonic_ns)


def test_the_trace_corrects_a_skewed_clock_instead_of_aligning_it(
    emulation_run: EmulationResult,
) -> None:
    run = load_run(emulation_run.run_dir)
    offsets = run.offsets(reference_host="driver")
    assert set(offsets) == set(WORKER_HOSTS)

    worker_c = offsets["worker-c"]
    # the deliberate skew is measured, not hidden
    assert worker_c.wall_offset_ns == pytest.approx(-2_500_000_000, abs=2e8)
    assert worker_c.monotonic_offset_ns == pytest.approx(1_250_000_000, abs=2e8)
    assert worker_c.uncertainty_ns > 0
    assert worker_c.method == "four_timestamp_ntp"

    worker_a = offsets["worker-a"]
    assert abs(worker_a.wall_offset_ns) < 2e8

    # uncorrected, worker-c's own wall stamps are in the past relative to the
    # driver's start - only the corrected timeline interleaves
    corrected = merge_timeline(run, offsets, reference_host="driver")
    worker_c_entries = [
        entry for entry in corrected if entry.event.host_id == "worker-c"
    ]
    assert worker_c_entries
    assert all(not entry.reference for entry in worker_c_entries)
    assert all(entry.uncertainty_ns > 0 for entry in worker_c_entries)


def test_the_summary_names_the_terminal_states_and_the_failure(
    emulation_run: EmulationResult,
) -> None:
    summary = emulation_run.summary
    assert "clock correction: reference driver" in summary
    assert "+/-" in summary
    assert "worker-c vs driver" in summary
    ok_request, negative_request = emulation_run.document["trace"]["requests"]
    assert f"request {ok_request}: terminal=completed" in summary
    assert CLEANUP_RECEIPT_TIMEOUT in summary
    assert f"request {negative_request}: terminal={CLEANUP_RECEIPT_TIMEOUT}" in summary
    assert "FIRST FAILURE" in summary
    assert (emulation_run.run_dir / "summary.txt").is_file()


def test_the_negative_case_never_stuck_the_request(
    emulation_run: EmulationResult,
) -> None:
    """Every request in the trace has a terminal event - no wedged request."""

    run = load_run(emulation_run.run_dir)
    for trace_file in run.requests:
        terminals = [
            event for event in trace_file.events if event.kind is EventKind.TERMINAL
        ]
        assert terminals, trace_file.subject
    negative_id = emulation_run.document["trace"]["requests"][1]
    negative_events = run.events_for_request(negative_id)
    terminal = [event for event in negative_events if event.kind is EventKind.TERMINAL][
        -1
    ]
    assert terminal.status.startswith(CLEANUP_RECEIPT_TIMEOUT)
    assert terminal.detail.get("never_stuck") is True
    assert terminal.detail.get("receipts_missing") == ["worker-c"]


# --------------------------------------------------------------------------- #
# evidence and the forbidden-surface rules
# --------------------------------------------------------------------------- #


def test_evidence_artifacts_are_written_to_the_runs_root(
    emulation_run: EmulationResult,
) -> None:
    run_dir = emulation_run.run_dir
    for name in (
        "run.json",
        "summary.txt",
        "emulation.json",
        "faults.json",
        "client_stream.txt",
        "exchanges.jsonl",
        "origin.log",
    ):
        assert (run_dir / name).is_file(), name
    assert (run_dir / "requests").is_dir() and (run_dir / "commands").is_dir()
    assert (run_dir / "homes" / "worker-a" / "identity.json").is_file()
    assert (run_dir / "tls" / "ca.pem").is_file()
    root = _runs_root()
    assert root in run_dir.parents or run_dir.parent == root

    document = json.loads((run_dir / "emulation.json").read_text(encoding="utf-8"))
    assert document["protocol"] == "mycelium.testbed.emulation.v1"
    assert document["origin"]["reused"]


def test_the_emulation_uses_no_public_hostname_and_no_sudo(
    emulation_run: EmulationResult,
) -> None:
    text = json.dumps(emulation_run.document)
    # PLAN D7 / packet: no public hostname, no quick tunnel, no DoH path, and
    # especially not the live hackathon demo
    assert "mycelium.now" not in text
    assert "trycloudflare" not in text
    assert "127.0.0.1:4351" not in text
    assert "sudo" not in text
    url = emulation_run.document["origin"]["url"]
    assert url.startswith("https://127.0.0.1:")
    assert emulation_run.document["origin"]["tls"]
    # the TLS material lives inside the run directory, not somewhere shared
    assert str(emulation_run.run_dir) in emulation_run.document["origin"]["tls_dir"]


def test_the_greedy_sequence_is_stable_across_runs(
    emulation_run: EmulationResult,
) -> None:
    """Golden greedy sequence: a change here must be noticed, not absorbed.

    The stage is greedy (the product's ``quantized_greedy_token_id``), so the
    completion is a deterministic function of the prompt, the snapshot and the MLX
    build.  Two independent full runs on this host produced this exact sequence
    (the acceptance run and ``runs/wp2-4/manual-cli.txt``); if a future MLX or
    snapshot change moves it, this test says so instead of letting it slide.
    """

    assert emulation_run.tokens == EMULATION_GOLDEN_TOKENS
    assert emulation_run.generated_text == "I'm sorry, but I can't"


def test_the_prompt_is_the_real_tokenizers_encoding(
    emulation_run: EmulationResult,
) -> None:
    """The prompt count in the trace is the real tokenizer's own answer."""

    from tokenizers import Tokenizer

    from mycelium_testbed.emulation import CHATML_SYSTEM

    run = load_run(emulation_run.run_dir)
    ok_request = emulation_run.document["trace"]["requests"][0]
    enqueue = [
        event
        for event in run.events_for_request(ok_request)
        if event.kind is EventKind.ENQUEUE
        and event.detail.get("origin") == "gateway_submit"
    ]
    assert enqueue, "no gateway_submit enqueue event"
    recorded_prompt = enqueue[0].detail["prompt"]
    recorded_tokens = enqueue[0].detail["prompt_tokens"]
    assert recorded_prompt == PROMPT

    tokenizer = Tokenizer.from_file(str(local_model_root() / "tokenizer.json"))
    text = (
        f"<|im_start|>system\n{CHATML_SYSTEM}<|im_end|>\n"
        f"<|im_start|>user\n{PROMPT}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )
    assert recorded_tokens == len(tokenizer.encode(text).ids)

    # the streamed deltas decode back to exactly the reported completion text
    ok_stream = emulation_run.document["streams"][ok_request]
    deltas = "".join(
        event["data"]["text"]
        for event in ok_stream["events"]
        if event["event"] == "token"
    )
    assert deltas == emulation_run.generated_text
    assert deltas == tokenizer.decode(list(emulation_run.tokens))
