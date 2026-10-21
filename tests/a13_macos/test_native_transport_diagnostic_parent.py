# SPDX-License-Identifier: AGPL-3.0-only
"""A stalled prefill hop must report the transport's verdict, not the symptom.

Regression context: the packaged two-Mac attempt failed with a native-authored
``fatal: prefill_completion_timeout``.  The native prefill wait polls only
``transport.fatal_error``, while a hop that fails inside the transport's
deferred forward path is recorded as a scoped, per-path, *non-fatal* event.  The
operator therefore learned the symptom and not the cause, and four build/run
cycles were spent bisecting.

``_bounded_native_diagnostics`` is the managed worker's only window onto the
native process, so it must be able to recover that verdict -- without ever
widening what leaves the node.  No model execution here.

These exercise the extraction contract only; the ``native-failure.private.json``
writer composes this dict, so asserting the dict shape is the boundary.
"""
import pytest

from mycelium_a13_macos.worker import (
    _NATIVE_TRANSPORT_COUNTER_KEYS,
    _bounded_native_diagnostics,
    _bounded_transport_diagnostic,
)

_GOOD = (
    "MYCELIUM-TRANSPORT-DIAG scoped_failure_code=destination_binding_mismatch"
    " remote_frames_sent=0 remote_frames_received=0"
    " router_frames_dispatched=0 duplicate_frames=0"
)
_TRACEBACK = (
    'Traceback (most recent call last):\n'
    '  File "/tmp/native/physical_inference_node.py", line 2865, in _infer_start\n'
    '    _require(status not in {"PREFILL", "LOCKED"}, "prefill_completion_timeout")\n'
    "NodeCommandError: prefill_completion_timeout\n"
)


def test_wellformed_line_yields_exact_verdict():
    result = _bounded_transport_diagnostic(_GOOD + "\n")
    assert result == {
        "protocol": "mycelium.a13.native_transport_diagnostic.v1",
        "scoped_failure_code": "destination_binding_mismatch",
        "remote_frames_sent": 0,
        "remote_frames_received": 0,
        "router_frames_dispatched": 0,
        "duplicate_frames": 0,
    }


def test_none_status_and_nonzero_counters_round_trip():
    line = (
        "MYCELIUM-TRANSPORT-DIAG scoped_failure_code=none"
        " remote_frames_sent=7 remote_frames_received=3"
        " router_frames_dispatched=2 duplicate_frames=0"
    )
    result = _bounded_transport_diagnostic(line)
    assert result["scoped_failure_code"] == "none"
    assert result["remote_frames_sent"] == 7
    assert result["remote_frames_received"] == 3
    assert result["router_frames_dispatched"] == 2


@pytest.mark.parametrize(
    "line",
    [
        # injection attempt in the status token
        "MYCELIUM-TRANSPORT-DIAG scoped_failure_code=$(rm -rf /)"
        " remote_frames_sent=0 remote_frames_received=0"
        " router_frames_dispatched=0 duplicate_frames=0",
        # oversized counter
        "MYCELIUM-TRANSPORT-DIAG scoped_failure_code=none"
        " remote_frames_sent=9999999999999 remote_frames_received=0"
        " router_frames_dispatched=0 duplicate_frames=0",
        # non-numeric counter
        "MYCELIUM-TRANSPORT-DIAG scoped_failure_code=none"
        " remote_frames_sent=abc remote_frames_received=0"
        " router_frames_dispatched=0 duplicate_frames=0",
        # missing key
        "MYCELIUM-TRANSPORT-DIAG scoped_failure_code=none remote_frames_sent=0",
        # extra unexpected key
        "MYCELIUM-TRANSPORT-DIAG scoped_failure_code=none"
        " remote_frames_sent=0 remote_frames_received=0"
        " router_frames_dispatched=0 duplicate_frames=0 endpoint_id=deadbeef",
        # duplicate key
        "MYCELIUM-TRANSPORT-DIAG scoped_failure_code=none scoped_failure_code=none"
        " remote_frames_sent=0 remote_frames_received=0"
        " router_frames_dispatched=0 duplicate_frames=0",
        # uppercase status
        "MYCELIUM-TRANSPORT-DIAG scoped_failure_code=FAILED"
        " remote_frames_sent=0 remote_frames_received=0"
        " router_frames_dispatched=0 duplicate_frames=0",
        # not a key=value body at all
        "MYCELIUM-TRANSPORT-DIAG blah",
    ],
)
def test_malformed_line_is_rejected_wholesale(line):
    assert _bounded_transport_diagnostic(line + "\n") is None


def test_no_line_is_backwards_compatible():
    result = _bounded_native_diagnostics(_TRACEBACK.encode())
    assert "transport" not in result
    assert result["exception_types"] == ["NodeCommandError"]
    assert result["frames"] == [
        {"file": "physical_inference_node.py", "line": 2865, "function": "_infer_start"}
    ]


def test_wellformed_line_is_attached_alongside_existing_fields():
    raw = (_TRACEBACK + _GOOD + "\n").encode()
    result = _bounded_native_diagnostics(raw)
    assert result["transport"]["scoped_failure_code"] == "destination_binding_mismatch"
    # pre-existing semantics must be untouched
    assert result["exception_types"] == ["NodeCommandError"]
    assert result["frames"][0]["function"] == "_infer_start"


def test_no_address_endpoint_or_key_material_can_survive():
    """The node's network identity must never ride along in the diagnostic."""
    hostile = (
        "endpoint_id=node-identity-84108c13e4197d8bbf9100cd111a586b\n"
        "addrs=10.37.129.2:60817,203.0.113.13:60817\n"
        "key=deadbeefdeadbeefdeadbeefdeadbeef\n"
        "prompt=hello world\n"
        "MYCELIUM-TRANSPORT-DIAG scoped_failure_code=forward_delivery_failed"
        " remote_frames_sent=1 remote_frames_received=0"
        " router_frames_dispatched=0 duplicate_frames=0\n"
    )
    result = _bounded_transport_diagnostic(hostile)
    assert set(result) == {"protocol", "scoped_failure_code", *_NATIVE_TRANSPORT_COUNTER_KEYS}
    blob = repr(result)
    for forbidden in ("84108c13", "10.37.129.2", "10.84.252.4", "203.0.113.13",
                      "deadbeef", "hello world", "endpoint_id", "prompt"):
        assert forbidden not in blob


@pytest.mark.parametrize(
    "raw",
    [b"", None, 0, object(), b"\xff\xfe\x00garbage", "not bytes", b"x" * 200_000],
)
def test_extraction_never_raises_on_hostile_input(raw):
    result = _bounded_native_diagnostics(raw)
    assert isinstance(result, dict)
    assert set(result) <= {"frames", "exception_types", "transport"}


def test_oversized_line_is_rejected():
    # Body longer than the 400-character bound cannot be accepted.
    long_line = (
        "MYCELIUM-TRANSPORT-DIAG scoped_failure_code=none"
        " remote_frames_sent=0 remote_frames_received=0"
        " router_frames_dispatched=0 duplicate_frames=0 "
        + "pad=" + "1" * 500
    )
    assert _bounded_transport_diagnostic(long_line + "\n") is None
