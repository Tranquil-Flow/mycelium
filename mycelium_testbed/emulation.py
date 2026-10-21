# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: E402 -- direct script execution needs the repository on sys.path.
"""Single-host two-worker C4->C5 emulation (PLAN 5.2 item 3).

What this module is
-------------------

A harness that runs the **whole C4->C5 chain on one Mac** as real processes:

```
  client  ──TLS──▶  relay "origin"  ──▶  https origin (SSE, self-signed CA)
                                            ▲  token events, one flush per token
                                            │
  driver  (route + gateway: fanout, budget arithmetic, terminal frame)
     ├── relay "worker-a" ──▶ worker process A   home A, identity A, Qwen layers [0,12)
     └── relay "worker-c" ──▶ worker process C   home C, identity C, Qwen layers [12,24) + head
```

* **Two worker processes, separate homes and identities.**  Each worker creates its
  own home directory, generates its own Ed25519 identity (the endpoint id is
  ``sha256`` of the public key, the shape the product's node identity uses) and
  reports it back on the control channel.  Nothing is shared but the run directory.
* **The real model, from disk.**  ``Qwen/Qwen2.5-0.5B-Instruct`` revision
  ``7ae557604adf67be50417f59c2c2f167def9a775``, read out of the local HuggingFace
  cache with ``mlx.core.load`` (no network, no download, no ``transformers``).  The
  real tokenizer is the snapshot's own ``tokenizer.json``.
* **A real ``https://`` origin on loopback**: TLS with a locally generated CA +
  leaf certificate, serving the SSE contract reused from
  ``research/scratch/cloudflared/sse_origin.py`` (see :func:`_reuse_notes`).
* **The promoted fault proxy** (:mod:`mycelium_testbed.faults`) sits on all three
  legs and shapes them to the measured round-50 latencies.

The model split is a real layer-range split: worker A holds the embedding matrix
and decoder layers ``[0, 12)``, worker C holds ``[12, 24)`` plus the final norm and
the **tied** output projection (``tie_word_embeddings: true`` in the snapshot's
``config.json``, so the head *is* ``model.embed_tokens.weight``).  Every token is a
real two-hop pipeline of real activations over the shaped relay path.

Budget arithmetic is imported, never re-derived: the route's fanout window, the
cancellation+cleanup budget, the receipt-poll cadence and the peer's advertised
cleanup bound all come from :mod:`mycelium_deadlines` (the WP2 registry), and the
D9 comparison is made with the registry's own
:func:`mycelium_deadlines.check_peer_advertised_cleanup_bound`.

The negative case
-----------------

Round 50 wedged because the route waits for a peer's cleanup receipt inside a
fanout window of ``2000 ms - 500 ms = 1500 ms`` while the peer advertises (and
takes) ``2000 ms``.  Here the "worker-c" relay arms a *receipt hold* on the
``down`` direction for exactly the cleanup exchange, so the receipt exists and
simply arrives after the window.  The request must then end in the **named**
failure ``route_cancellation_cleanup_timeout|<blockers>`` inside the 2 s budget -
never a stuck request.  The D9 *fix* (validating the advertised bound at startup
qualification) belongs to the A13 lane / WP5; this module only reproduces the
failure shape and reports the registry's verdict as evidence.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import random
import resource
import socket
import ssl
from threading import Event as ThreadEvent
from threading import Thread
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Mapping, Sequence

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mycelium_deadlines import (  # noqa: E402
    CANCEL_AND_CLEANUP_BUDGET_SECONDS,
    CLEANUP_RECEIPT_POLL_SECONDS,
    PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS,
    advertised_maximum_total_cleanup_ms,
    check_peer_advertised_cleanup_bound,
    route_cleanup_fanout_window_ms,
)
from mycelium_router.decoding import quantized_greedy_token_id  # noqa: E402
from mycelium_testbed.faults import (  # noqa: E402
    ROUND50_MEASURED,
    ROUND50_RECEIPT_POLL_MAX_MS,
    ROUND50_RECEIPT_POLL_MEAN_MS,
    ROUND50_RECEIPT_POLL_P95_MS,
    ROUND50_UNMEASURED,
    FaultProxyHandle,
    Round50LegConfig,
    round50_legs,
    start_proxy,
)
from mycelium_testbed.trace import (  # noqa: E402
    EventKind,
    ExchangeSample,
    HostClock,
    TraceError,
    TraceRecorder,
    TraceRunDirectory,
    estimate_offsets_by_host,
    load_run,
    merge_timeline,
    summarise,
)

__all__ = [
    "A3_LAYER_SPLIT",
    "CHATML_SYSTEM",
    "CLEANUP_RECEIPT_TIMEOUT",
    "DETERMINISTIC_MEMORY_CHECKS",
    "DRIVER_HOST",
    "EmulationError",
    "EmulationResult",
    "KERNELS_RUNTIME_LOADER",
    "KERNELS_TESTBED",
    "LAYER_SPLIT",
    "LOAD_SENSITIVE_CHECKS",
    "MODEL_ID",
    "MODEL_PROFILES",
    "MODEL_REVISION",
    "ModelProfile",
    "PROMPT",
    "REPRESENTATION_ROWWISE_INT8",
    "REPRESENTATION_SOURCE",
    "RSS_RECLAIM_TOLERANCE",
    "RSS_SAMPLING_METHOD",
    "STAGE_ACCOUNTING_BASIS",
    "TOKEN_STEPS",
    "USEFUL_MODEL_ID",
    "USEFUL_MODEL_REVISION",
    "WORKER_HOSTS",
    "WorkerMemoryVerdict",
    "build_model_root",
    "emulation_document",
    "evaluate_worker_memory_envelope",
    "local_model_root",
    "main",
    "model_present",
    "pilot_profile",
    "placement_expected_bytes",
    "read_safetensors_tensor_bytes",
    "read_safetensors_tensor_meta",
    "run_emulation",
    "snapshot_memory_facts",
    "useful_model_profile",
    "worker_main",
]


class EmulationError(RuntimeError):
    """A stop condition: something the emulation cannot honestly work around."""


# --------------------------------------------------------------------------- #
# the real model on disk
# --------------------------------------------------------------------------- #

MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
#: The D1 useful model (WP7).  Same on-disk local cache, same repo kernels, but the
#: ``mycelium.rowwise_symmetric_int8.v1`` representation and the A3 route split.
USEFUL_MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"
USEFUL_MODEL_REVISION = "a09a35458c702b33eeacc393d103063234e8bc28"
#: The prompt the existing iroh e2e harness uses (``tests/e2e_request_iroh/harness.py``).
PROMPT = "deterministic local moonlight"
CHATML_SYSTEM = "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."
#: Layers per placement: [0, 12) on worker-a, [12, 24) + head on worker-c.
LAYER_SPLIT: tuple[tuple[str, int, int], ...] = (
    ("worker-a", 0, 12),
    ("worker-c", 12, 24),
)
#: The split the executed A3 route used for the D1 useful model: layers [0, 21) on the
#: first placement, [21, 28) plus the final norm and the output projection on the last.
A3_LAYER_SPLIT: tuple[tuple[str, int, int], ...] = (
    ("worker-a", 0, 21),
    ("worker-c", 21, 28),
)
#: Representation labels.  ``rowwise_symmetric_int8`` is the repository's own weight
#: codec (``runtime_loader._quantize_qwen2_mlx_tensor``) - never an ``mlx-lm``
#: quantisation, and never a second quantiser invented here.
REPRESENTATION_SOURCE = "source-artifact-native-dtype"
REPRESENTATION_ROWWISE_INT8 = "mycelium.rowwise_symmetric_int8.v1"
#: Which Qwen2 block kernel executes a placement's range.
KERNELS_TESTBED = "mycelium_testbed.qwen_stage_mlx"
KERNELS_RUNTIME_LOADER = "runtime_loader._qwen2_block"
#: Tokens the successful request streams; enough to prove the pipeline runs per token.
TOKEN_STEPS = 8
#: Tokens the negative case streams before the cancellation arrives.
NEGATIVE_TOKEN_STEPS = 3

DRIVER_HOST = "driver"
WORKER_HOSTS: tuple[str, ...] = ("worker-a", "worker-c")

#: The route-side named failure.  Same shape the product raises at
#: ``mycelium_live/route.py:2696/2757/2767``:
#: ``route_cancellation_cleanup_timeout|<blockers>``.
CLEANUP_RECEIPT_TIMEOUT = "route_cancellation_cleanup_timeout"

#: The two tensors the two placements share or close out.
EMBED_TOKENS_TENSOR = "model.embed_tokens.weight"
FINAL_NORM_TENSOR = "model.norm.weight"
#: The untied output projection.  ``Qwen2.5-7B-Instruct`` ships one; the 0.5B ties.
LM_HEAD_TENSOR = "lm_head.weight"


def _repo_model_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """The snapshot config in the shape ``runtime_loader._qwen2_block`` expects."""

    heads = int(config["num_attention_heads"])
    hidden = int(config["hidden_size"])
    head_dim = config.get("head_dim")
    if not isinstance(head_dim, int) or head_dim <= 0:
        head_dim = hidden // heads
    return {
        "n_layer": int(config["num_hidden_layers"]),
        "n_embd": hidden,
        "n_head": heads,
        "n_kv_head": int(config["num_key_value_heads"]),
        "n_inner": int(config["intermediate_size"]),
        "vocab_size": int(config["vocab_size"]),
        "rms_norm_epsilon": float(config["rms_norm_eps"]),
        "rope_theta": float(config["rope_theta"]),
        "head_dim": head_dim,
        "activation_function": "silu",
        "tie_word_embeddings": bool(config.get("tie_word_embeddings", False)),
        "source_dtype": str(config.get("torch_dtype") or config.get("dtype")),
    }


def _resident_bytes_of(value: Any) -> int:
    """The bytes one loaded tensor occupies, in whichever representation it is.

    An ``Int8RowwiseWeight`` is the int8 codes plus one float32 scale per output
    row - the accounting the A3 record's ``resident_weight_bytes`` uses.
    """

    if hasattr(value, "values") and hasattr(value, "scales"):
        return int(value.values.size) + int(value.scales.size) * 4
    return int(value.size) * int(value.itemsize)


def _representation_dtype_label(value: Any, fallback: str) -> str:
    if hasattr(value, "values") and hasattr(value, "scales"):
        return "int8+float32-scale"
    return str(value.dtype)


def _representation_tensor_bytes(
    dtype: str, shape: Sequence[int], name: str, representation: str
) -> int:
    """The bytes a tensor occupies in ``representation``, from header metadata only.

    Independent of what any stage loaded: this is the checkpoint's own statement of
    the tensor's shape and dtype, put through the codec's arithmetic.
    """

    elements = 1
    for dimension in shape:
        elements *= int(dimension)
    if representation == REPRESENTATION_ROWWISE_INT8:
        if name.endswith(".weight") and len(shape) == 2:
            # int8 codes, one per element, plus one float32 scale per output row
            return elements + int(shape[0]) * 4
        return elements * 4
    return elements * _SOURCE_DTYPE_BYTES.get(str(dtype), 0)


#: Element size of the source dtypes the snapshots use, for header-only arithmetic.
_SOURCE_DTYPE_BYTES: dict[str, int] = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "F8_E4M3": 1,
    "F8_E5M2": 1,
    "I16": 2,
    "U16": 2,
    "F16": 2,
    "BF16": 2,
    "I32": 4,
    "U32": 4,
    "F32": 4,
    "I64": 8,
    "U64": 8,
    "F64": 8,
}

# --------------------------------------------------------------------------- #
# the memory envelope: accounting (deterministic) vs observation (sampled)
# --------------------------------------------------------------------------- #
#: What the stage's resident bytes *are*: its own load accounting, summed over the
#: tensors it checked in.  Deterministic, and the only basis a check may use.
STAGE_ACCOUNTING_BASIS = "stage_load_accounting:sum_of_loaded_tensor_bytes"
#: How the driver observes worker memory.  The quantity is sampled from outside the
#: worker, so it is an observation of the OS and of machine load, not of the stage.
RSS_SAMPLING_METHOD = (
    "ps -o rss= -p <pid> every 0.1s while the requests ran, peak kept "
    "(external sampler; sampled OS quantity, so it is load-sensitive)"
)
#: Under memory pressure macOS reclaims and compresses pages, so a sampled peak may
#: legitimately land well below the bytes the stage loaded.  The tolerance is
#: therefore *stated* in the evidence and never required: an RSS bound is a sanity
#: observation, not a verdict, and it must never decide the suite's colour.
RSS_RECLAIM_TOLERANCE = 0.5
#: The checks that read a sampled quantity.  Advisory by construction.
LOAD_SENSITIVE_CHECKS: tuple[str, ...] = ("rss_sanity_bound",)
#: The checks that read only what the stage (and the snapshot header) account for.
DETERMINISTIC_MEMORY_CHECKS: tuple[str, ...] = (
    "accounted_resident_bytes_positive",
    "resident_bytes_equal_tensor_byte_sum",
    "accounted_bytes_match_snapshot_header",
    "accounted_tensors_match_snapshot_header",
)


def snapshot_root(model_id: str, revision: str) -> Path:
    """The exact on-disk snapshot for ``model_id`` at ``revision``."""

    hub = (
        Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    )
    return hub / f"models--{model_id.replace('/', '--')}" / "snapshots" / revision


@dataclass(frozen=True)
class ModelProfile:
    """One model + representation + split the emulation can run.

    The default profile is the pilot's 0.5B chain and is byte-for-byte the behaviour
    this module had before WP7-2: the tests in ``tests/testbed`` pin it.  The
    useful-model profile is additive - it selects the D1 model, the repository's
    rowwise-symmetric-int8 codec and the A3 route split.
    """

    name: str
    model_id: str
    revision: str
    layer_split: tuple[tuple[str, int, int], ...]
    expected_layers: int
    representation: str
    kernels: str
    runtime_dtype: str
    owned_components: tuple[str, ...]
    sharded: bool

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node for node, _start, _end in self.layer_split)

    @property
    def int8(self) -> bool:
        return self.representation == REPRESENTATION_ROWWISE_INT8

    @property
    def cacheless(self) -> bool:
        """The repo block kernel has no KV-cache interface, so it re-runs the range.

        ``runtime_loader._qwen2_block`` builds its own causal mask over the whole
        sequence and ``execute_loaded_stage`` documents the contract: "There is
        intentionally no KV-cache interface, so callers must pass the complete
        sequence on every invocation."  A placement executed with those kernels
        therefore reprocesses the running sequence each step.
        """

        return self.kernels == KERNELS_RUNTIME_LOADER

    def model_root(self) -> Path:
        return snapshot_root(self.model_id, self.revision)

    def owns_head(self, end_layer: int) -> bool:
        return end_layer == self.expected_layers

    def to_document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model_id": self.model_id,
            "revision": self.revision,
            "representation": self.representation,
            "kernels": self.kernels,
            "runtime_dtype": self.runtime_dtype,
            "layer_split": [list(item) for item in self.layer_split],
            "expected_layers": self.expected_layers,
            "owned_components": list(self.owned_components),
            "cacheless": self.cacheless,
            "sharded": self.sharded,
        }


def pilot_profile() -> ModelProfile:
    return ModelProfile(
        name="pilot-0.5b",
        model_id=MODEL_ID,
        revision=MODEL_REVISION,
        layer_split=LAYER_SPLIT,
        expected_layers=24,
        representation=REPRESENTATION_SOURCE,
        kernels=KERNELS_TESTBED,
        runtime_dtype="bfloat16",
        owned_components=("input_embedding", "decoder", "final_norm", "lm_head"),
        sharded=False,
    )


def useful_model_profile() -> ModelProfile:
    """The D1 useful model over the A3 split, in the repository's int8 path."""

    return ModelProfile(
        name="a3-useful-7b-rowwise-int8",
        model_id=USEFUL_MODEL_ID,
        revision=USEFUL_MODEL_REVISION,
        layer_split=A3_LAYER_SPLIT,
        expected_layers=28,
        representation=REPRESENTATION_ROWWISE_INT8,
        kernels=KERNELS_RUNTIME_LOADER,
        runtime_dtype="float32",
        owned_components=("input_embedding", "decoder", "final_norm", "lm_head"),
        sharded=True,
    )


MODEL_PROFILES = {
    "pilot": pilot_profile,
    "useful": useful_model_profile,
}


def local_model_root() -> Path:
    """The exact on-disk snapshot: no download path exists in this module."""

    return snapshot_root(MODEL_ID, MODEL_REVISION)


def build_model_root(home: Path | str) -> Path:
    """Build the snapshot path from an explicit HuggingFace hub root."""

    return (
        Path(home)
        / "hub"
        / f"models--{MODEL_ID.replace('/', '--')}"
        / "snapshots"
        / MODEL_REVISION
    )


def model_present(model_root: Path, *, sharded: bool = False) -> bool:
    root = Path(model_root)
    if not (root / "tokenizer.json").is_file():
        return False
    if sharded:
        index = root / "model.safetensors.index.json"
        if not index.is_file():
            return False
        weight_map = json.loads(index.read_text(encoding="utf-8")).get("weight_map")
        if not isinstance(weight_map, dict) or not weight_map:
            return False
        return all((root / name).is_file() for name in set(weight_map.values()))
    return (root / "model.safetensors").is_file()


def _require_model(model_root: Path, *, sharded: bool = False) -> Path:
    if not model_present(model_root, sharded=sharded):
        raise EmulationError(
            f"pilot_model_missing:{model_root}:expected {MODEL_ID} at revision "
            f"{MODEL_REVISION} already on disk (this harness never downloads)"
        )
    return Path(model_root)


# --------------------------------------------------------------------------- #
# SSE origin: the contract reused from the WP2 cloudflared research
# --------------------------------------------------------------------------- #

#: Headers copied verbatim from ``research/scratch/cloudflared/sse_origin.py``.
SSE_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Type", "text/event-stream; charset=utf-8"),
    ("Cache-Control", "no-cache, no-store"),
    ("X-Accel-Buffering", "no"),
)


def _reuse_notes() -> tuple[str, ...]:
    """Exactly what was reused from the WP2 cloudflared research scratch area."""

    return (
        "research/scratch/cloudflared/sse_origin.py: the SSE response header "
        "contract (text/event-stream, no-cache/no-store, X-Accel-Buffering: no), "
        "HTTP/1.1 with no Content-Length, one flush per event, the ': heartbeat' "
        "comment line and the 'event: done' terminator, and the /health route.",
        "research/scratch/cloudflared/sse_client.py: the client-side timing "
        "contract - read byte-by-byte and report max_inter_byte_gap, so "
        "'streaming' is a number rather than an opinion.",
        "research/scratch/cloudflared/spike-output.txt §11: the measured "
        "max_inter_byte_gap=1.005s at freq=1.0s for that origin, which is the "
        "shape this emulation reproduces with its own token cadence.",
        "NOT reused verbatim, and why: the research origin itself was plain HTTP on "
        "127.0.0.1:8791 because the named tunnel terminated TLS at Cloudflare's "
        "edge.  A public hostname, a quick tunnel and the live mycelium-demo tunnel "
        "are all forbidden here (PLAN D7, packet), so the same handler contract is "
        "served over TLS with a locally generated CA + leaf instead.",
        "CORRECTION to the research origin's comment: it says the missing "
        "Content-Length makes the body 'chunked transfer-encoding'.  Measured here, "
        "http.server does not chunk on its own - the body is close-delimited "
        "(transfer-encoding is absent; the client reads to EOF).  The header "
        "contract and the one-flush-per-event rule are unchanged; only that "
        "comment's framing claim is wrong.",
    )


class SseOrigin:
    """A real HTTPS SSE origin on loopback, one stream per request.

    The handler body is the research origin's: same status line, same headers,
    same one-flush-per-event rule, same heartbeat comment and ``done`` terminator.
    The transport is TLS instead of plain HTTP (see :func:`_reuse_notes`).
    """

    def __init__(self, tls_dir: Path, *, host: str = "127.0.0.1") -> None:
        self.tls_dir = Path(tls_dir)
        self.host = host
        self.cert_path = self.tls_dir / "leaf.pem"
        self.key_path = self.tls_dir / "leaf.key"
        self.ca_path = self.tls_dir / "ca.pem"
        self.log_path = self.tls_dir.parent / "origin.log"
        self._server: Any = None
        self._thread: Thread | None = None
        self._context: ssl.SSLContext | None = None
        self.port: int | None = None
        # one live exchange at a time (the emulation is deliberately sequential)
        self._events: queue.Queue[str | None] | None = None
        self._request_arrived = ThreadEvent()
        self._lock = threading.Lock()

    # -- certificates ------------------------------------------------------- #

    def ensure_certificates(self) -> None:
        """Generate a local CA and a leaf for ``localhost``/``127.0.0.1``."""

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        self.tls_dir.mkdir(parents=True, exist_ok=True)
        import datetime as _datetime

        now = _datetime.datetime.now(_datetime.timezone.utc)
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "mycelium-emulated-local-ca")]
        )
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _datetime.timedelta(minutes=5))
            .not_valid_after(now + _datetime.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=False,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf_cert = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            )
            .issuer_name(ca_name)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _datetime.timedelta(minutes=5))
            .not_valid_after(now + _datetime.timedelta(days=2))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            # OpenSSL 3 refuses a chain whose leaf has no Authority Key Identifier.
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        _write_private(self.ca_path, ca_cert.public_bytes(serialization.Encoding.PEM))
        _write_private(
            self.cert_path, leaf_cert.public_bytes(serialization.Encoding.PEM)
        )
        _write_private(
            self.key_path,
            leaf_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
        )

    # -- serving ------------------------------------------------------------ #

    def start(self) -> int:
        self.ensure_certificates()
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(
            certfile=str(self.cert_path), keyfile=str(self.key_path)
        )
        origin = self

        class Handler(_SseHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                origin._log("origin: " + format % args)

        httpd = ThreadingHTTPServer((self.host, 0), Handler)
        httpd.daemon_threads = True
        httpd.origin = origin  # type: ignore[attr-defined]
        httpd.socket = self._context.wrap_socket(httpd.socket, server_side=True)
        self._server = httpd
        self.port = int(httpd.socket.getsockname()[1])
        self._thread = Thread(
            target=httpd.serve_forever, name="sse-origin", daemon=True
        )
        self._thread.start()
        self._log(
            f"origin listening on https://{self.host}:{self.port} (TLS, local CA)"
        )
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- one exchange ------------------------------------------------------- #

    @contextmanager
    def exchange(self):
        """Attach a fresh event queue for one client stream."""

        events: queue.Queue[str | None] = queue.Queue()
        with self._lock:
            self._events = events
            self._request_arrived.clear()
        try:
            yield events
        finally:
            with self._lock:
                self._events = None

    def wait_for_client(self, timeout: float = 10.0) -> bool:
        return self._request_arrived.wait(timeout)

    def client_cert_path(self) -> Path:
        return self.ca_path

    # -- internals ---------------------------------------------------------- #

    def _log(self, line: str) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def _current_events(self) -> queue.Queue[str | None] | None:
        with self._lock:
            return self._events

    def _mark_arrived(self) -> None:
        self._request_arrived.set()


class _SseHandler(BaseHTTPRequestHandler):
    """The research origin's handler contract, driven by the emulation's queue."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        origin: SseOrigin = self.server.origin  # type: ignore[attr-defined]
        if self.path.startswith("/health"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith("/stream"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        events = origin._current_events()
        if events is None:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(200)
        for name, value in SSE_HEADERS:
            self.send_header(name, value)
        # HTTP/1.1 and no Content-Length: the body is close-delimited, so the
        # client reads to EOF.  (The research origin's comment claims this makes
        # the body chunked; http.server does not chunk by itself - see
        # _reuse_notes, which records the correction.)
        self.end_headers()
        origin._mark_arrived()
        try:
            while True:
                item = events.get()
                if item is None:
                    break
                self.wfile.write(item.encode("utf-8"))
                self.wfile.flush()  # one flush per event: the whole point
                origin._log(
                    "origin: flushed %d bytes for %s"
                    % (len(item), self.path.split("request_id=")[-1])
                )
        except (BrokenPipeError, ConnectionResetError):
            origin._log("origin: client went away mid-stream")
            return
        # heartbeat comment line, the cheap keepalive a proxy cannot strip, then
        # the terminator - both exactly the research origin's tail.
        self.wfile.write(b": heartbeat\n\n")
        self.wfile.flush()
        self.wfile.write(b"event: done\ndata: {}\n\n")
        self.wfile.flush()


def sse_event(name: str, payload: Mapping[str, Any]) -> str:
    """One SSE frame, the shape the research origin wrote (``event:`` + ``data:``)."""

    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"event: {name}\ndata: {body}\n\n"


# --------------------------------------------------------------------------- #
# TLS client: byte-by-byte, the research probe's measurement contract
# --------------------------------------------------------------------------- #


@dataclass
class ClientStreamMeasurements:
    """What the client observed, measured the way ``sse_client.py`` measured."""

    status: int
    content_type: str | None
    transfer_encoding: str | None
    x_accel_buffering: str | None
    tls_version: str | None
    cipher: str | None
    peer_cert_subject: str | None
    elapsed_s: float
    bytes_read: int
    newline_chunks: int
    max_inter_byte_gap_s: float
    events: tuple[dict[str, Any], ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "content_type": self.content_type,
            "transfer_encoding": self.transfer_encoding,
            "x_accel_buffering": self.x_accel_buffering,
            "tls_version": self.tls_version,
            "cipher": self.cipher,
            "peer_cert_subject": self.peer_cert_subject,
            "elapsed_s": round(self.elapsed_s, 6),
            "bytes_read": self.bytes_read,
            "newline_chunks": self.newline_chunks,
            "max_inter_byte_gap_s": round(self.max_inter_byte_gap_s, 6),
            "events": list(self.events),
        }


def _read_stream_over_tls(
    host: str,
    port: int,
    ca_path: Path,
    *,
    path: str,
    timeout: float,
) -> ClientStreamMeasurements:
    """Byte-by-byte TLS SSE read, in the research probe's measurement shape.

    Uses a raw ``ssl`` socket rather than ``http.client``: the origin sends
    HTTP/1.1 with no ``Content-Length`` (the research origin's contract, so the
    body is close-delimited), and reading the socket one byte at a time is exactly
    what ``research/scratch/cloudflared/sse_client.py`` did.  Runs in a worker
    thread (``asyncio.to_thread``).
    """

    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
    raw_socket = socket.create_connection((host, port), timeout=timeout)
    tls = context.wrap_socket(raw_socket, server_hostname=host)
    try:
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Accept: text/event-stream\r\nConnection: close\r\n\r\n"
        )
        tls.sendall(request.encode("ascii"))
        tls_version = tls.version()
        cipher = (tls.cipher() or (None,))[0]
        peer = tls.getpeercert()
        peer_subject = str(peer.get("subject")) if peer else None

        started = time.time()
        last = started
        max_gap = 0.0
        total_bytes = 0
        newlines = 0
        header_blob = b""
        status = 0
        headers: dict[str, str] = {}
        in_headers = True
        buffer = b""
        events: list[dict[str, Any]] = []
        while True:
            chunk = tls.recv(1)
            if not chunk:
                break
            now = time.time()
            max_gap = max(max_gap, now - last)
            last = now
            total_bytes += 1
            if in_headers:
                header_blob += chunk
                if header_blob.endswith(b"\r\n\r\n"):
                    head, _, _rest = header_blob.partition(b"\r\n\r\n")
                    lines = head.decode("latin-1").split("\r\n")
                    parts = lines[0].split()
                    status = int(parts[1]) if len(parts) > 1 else 0
                    for line in lines[1:]:
                        name, separator, value = line.partition(":")
                        if separator:
                            headers[name.strip().lower()] = value.strip()
                    in_headers = False
                continue
            if chunk == b"\n":
                newlines += 1
            buffer += chunk
            while b"\n\n" in buffer:
                raw_event, _, buffer = buffer.partition(b"\n\n")
                name = None
                data = None
                for line in raw_event.decode("utf-8", "replace").splitlines():
                    if line.startswith("event: "):
                        name = line[len("event: ") :]
                    elif line.startswith("data: "):
                        data = line[len("data: ") :]
                if name is not None and data is not None:
                    try:
                        parsed: Any = json.loads(data)
                    except json.JSONDecodeError:
                        parsed = data
                    events.append(
                        {"event": name, "data": parsed, "t_s": round(now - started, 6)}
                    )
    finally:
        try:
            tls.close()
        except OSError:
            pass
    return ClientStreamMeasurements(
        status=status,
        content_type=headers.get("content-type"),
        transfer_encoding=headers.get("transfer-encoding"),
        x_accel_buffering=headers.get("x-accel-buffering"),
        tls_version=tls_version,
        cipher=cipher,
        peer_cert_subject=peer_subject,
        elapsed_s=last - started,
        bytes_read=total_bytes,
        newline_chunks=newlines,
        max_inter_byte_gap_s=max_gap,
        events=tuple(events),
    )


# --------------------------------------------------------------------------- #
# identity and the worker home
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorkerIdentity:
    node_id: str
    endpoint_id: str
    public_key_pem: str
    created_at: str
    home: str

    def to_document(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "endpoint_id": self.endpoint_id,
            "created_at": self.created_at,
            "home": self.home,
        }


def _write_private(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def establish_identity(home: Path, node_id: str) -> WorkerIdentity:
    """Create (or reload) this worker's own home and Ed25519 identity."""

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    home.mkdir(parents=True, exist_ok=True)
    key_path = home / "identity.key"
    document_path = home / "identity.json"
    if key_path.is_file():
        private = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
    else:
        private = ed25519.Ed25519PrivateKey.generate()
        _write_private(
            key_path,
            private.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
        )
    public = private.public_key()
    public_bytes = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    identity = WorkerIdentity(
        node_id=node_id,
        endpoint_id="sha256:" + hashlib.sha256(public_bytes).hexdigest(),
        public_key_pem=public.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii"),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        home=str(home),
    )
    document_path.write_text(
        json.dumps(identity.to_document(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return identity


# --------------------------------------------------------------------------- #
# the real Qwen2.5 stages, in MLX
# --------------------------------------------------------------------------- #


class QwenStageError(RuntimeError):
    """The stage cannot be built from the snapshot (never silently approximated)."""


class QwenStage:
    """A real layer range of a Qwen2.5 snapshot, executed with MLX.

    ``owns_embed`` builds the input embedding; ``owns_head`` applies the final norm
    and the output projection and returns a token id instead of a hidden state.

    Two execution kernels and two weight representations are supported, selected by
    :class:`ModelProfile`:

    * ``kernels=KERNELS_TESTBED`` (default) - this module's own per-head MLX block
      with a KV cache, reading the snapshot's native dtype straight out of
      ``model.safetensors``.  That is the pilot 0.5B path and is unchanged.
    * ``kernels=KERNELS_RUNTIME_LOADER`` - the repository's own Qwen2 block
      (``runtime_loader._qwen2_block`` / ``_qwen2_embedding`` / ``_qwen2_linear`` /
      ``_rms_norm``) with the repository's own rowwise-symmetric-int8 codec
      (``runtime_loader._quantize_qwen2_mlx_tensor``).  This path has no KV cache,
      exactly like ``runtime_loader.execute_loaded_stage``, so it reprocesses the
      running sequence on every step.
    """

    def __init__(
        self,
        *,
        model_root: Path,
        node_id: str,
        start_layer: int,
        end_layer: int,
        owns_embed: bool,
        owns_head: bool,
        model_id: str = MODEL_ID,
        model_revision: str = MODEL_REVISION,
        representation: str = REPRESENTATION_SOURCE,
        kernels: str = KERNELS_TESTBED,
        expected_layers: int | None = None,
        sharded: bool = False,
    ) -> None:
        self.model_root = Path(model_root)
        self.node_id = node_id
        self.start_layer = start_layer
        self.end_layer = end_layer
        self.owns_embed = owns_embed
        self.owns_head = owns_head
        self.model_id = model_id
        self.model_revision = model_revision
        self.representation = representation
        self.kernels = kernels
        self.expected_layers = expected_layers
        self.sharded = sharded
        self._weights: dict[str, Any] = {}
        self._config: dict[str, Any] = {}
        self._repo_config: dict[str, Any] = {}
        self._kv: dict[int, tuple[Any, Any]] = {}
        self._sequence_seen = 0
        self.resident_bytes = 0
        self.bytes_by_dtype: dict[str, int] = {}
        self.tensor_count = 0
        self.head_tied_to_embeddings = False
        self.head_tensor = ""

    @property
    def int8(self) -> bool:
        return self.representation == REPRESENTATION_ROWWISE_INT8

    @property
    def cacheless(self) -> bool:
        return self.kernels == KERNELS_RUNTIME_LOADER

    # -- loading ------------------------------------------------------------ #

    def _layer_prefixes(self) -> tuple[str, ...]:
        return tuple(
            f"model.layers.{index}."
            for index in range(self.start_layer, self.end_layer)
        )

    def _head_key(self) -> str:
        """The output projection's tensor key, tied or not.

        ``Qwen/Qwen2.5-0.5B-Instruct`` ties its embeddings, so its head *is*
        ``model.embed_tokens.weight``.  ``Qwen/Qwen2.5-7B-Instruct`` does not
        (``tie_word_embeddings: false``) and ships a distinct ``lm_head.weight``;
        the two are not byte-identical in this snapshot, so the head is loaded
        explicitly rather than assumed tied.
        """

        if bool(self._config.get("tie_word_embeddings")):
            return EMBED_TOKENS_TENSOR
        if not (self.model_root / "config.json").is_file():  # pragma: no cover
            raise QwenStageError("untied_head_without_config")
        return LM_HEAD_TENSOR

    def _wanted_names(self, available: Mapping[str, Any]) -> list[str]:
        prefixes = self._layer_prefixes()
        wanted = [
            name
            for name in available
            if any(name.startswith(prefix) for prefix in prefixes)
        ]
        if self.owns_embed:
            wanted.append(EMBED_TOKENS_TENSOR)
        if self.owns_head:
            wanted.append(FINAL_NORM_TENSOR)
            head = self._head_key()
            if head not in available:
                raise QwenStageError(f"head_tensor_missing:{head}")
            if head not in wanted:
                wanted.append(head)
        return sorted(wanted)

    def _convert(self, name: str, value: Any, mx: Any) -> Any:
        """One tensor, in the stage's representation - the repository's own codec."""

        if self.int8 and name.endswith(".weight") and len(value.shape) == 2:
            from runtime_loader import _quantize_qwen2_mlx_tensor

            # the repo codec: scale = max|row| / 127, round-half-away-from-zero,
            # clipped to [-127, 127], applied per output row.  Not an mlx-lm
            # quantisation and not a second quantiser.
            return _quantize_qwen2_mlx_tensor(name, value, mx)
        dtype = mx.bfloat16 if self.kernels == KERNELS_TESTBED else mx.float32
        converted = value.astype(dtype)
        mx.eval(converted)
        return converted

    def _read_snapshot(self, mx: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Load exactly this placement's tensors, shard by shard when needed."""

        if not self.sharded:
            weights_path = self.model_root / "model.safetensors"
            if not weights_path.is_file():
                raise EmulationError(f"qwen_snapshot_incomplete:{self.model_root}")
            blob = dict(mx.load(str(weights_path)))
            wanted = self._wanted_names(blob)
            tensors = {name: self._convert(name, blob[name], mx) for name in wanted}
            records = [
                {
                    "file": weights_path.name,
                    "tensors": len(tensors),
                    "bytes": weights_path.stat().st_size,
                }
            ]
            del blob
            mx.clear_cache()
            return tensors, records

        index_path = self.model_root / "model.safetensors.index.json"
        if not index_path.is_file():
            raise EmulationError(f"qwen_snapshot_index_missing:{self.model_root}")
        weight_map = json.loads(index_path.read_text(encoding="utf-8"))["weight_map"]
        wanted = self._wanted_names(weight_map)
        by_shard: dict[str, list[str]] = {}
        for name in wanted:
            by_shard.setdefault(weight_map[name], []).append(name)
        tensors: dict[str, Any] = {}
        records: list[dict[str, Any]] = []
        for shard in sorted(by_shard):
            path = self.model_root / shard
            if not path.is_file():
                raise EmulationError(f"qwen_shard_missing:{path}")
            started = time.time()
            blob = dict(mx.load(str(path)))
            quantized = 0
            for name in by_shard[shard]:
                if name not in blob:
                    raise EmulationError(f"qwen_shard_tensor_missing:{shard}:{name}")
                tensors[name] = self._convert(name, blob[name], mx)
                if self.int8 and name.endswith(".weight") and blob[name].ndim == 2:
                    quantized += 1
            del blob
            mx.clear_cache()
            records.append(
                {
                    "file": shard,
                    "tensors": len(by_shard[shard]),
                    "quantized": quantized,
                    "bytes": path.stat().st_size,
                    "seconds": round(time.time() - started, 2),
                }
            )
        return tensors, records

    def load(self) -> dict[str, Any]:
        import mlx.core as mx

        config_path = self.model_root / "config.json"
        if not config_path.is_file():
            raise EmulationError(f"qwen_snapshot_incomplete:{self.model_root}")
        self._config = json.loads(config_path.read_text(encoding="utf-8"))
        self._repo_config = _repo_model_config(self._config)
        self.head_tied_to_embeddings = bool(
            self._config.get("tie_word_embeddings")
        ) and self.owns_head
        self.head_tensor = self._head_key() if self.owns_head else ""
        self._weights, shard_records = self._read_snapshot(mx)
        self.tensor_count = len(self._weights)
        resident = 0
        bytes_by_dtype: dict[str, int] = {}
        for name, value in self._weights.items():
            mx.eval(value.values if self.int8 and hasattr(value, "values") else value)
            tensor_bytes = _resident_bytes_of(value)
            resident += tensor_bytes
            dtype = _representation_dtype_label(
                value, "int8" if self.int8 else self.kernels
            )
            bytes_by_dtype[dtype] = bytes_by_dtype.get(dtype, 0) + tensor_bytes
        self.resident_bytes = resident
        self.bytes_by_dtype = bytes_by_dtype
        expected = self._config.get("num_hidden_layers")
        if not isinstance(expected, int):
            raise QwenStageError(f"unexpected_num_hidden_layers:{expected}")
        if self.expected_layers is None:
            if expected != 24:
                raise QwenStageError(f"unexpected_num_hidden_layers:{expected}")
        elif expected != self.expected_layers:
            raise QwenStageError(
                f"unexpected_num_hidden_layers:{expected}!={self.expected_layers}"
            )
        return {
            "node_id": self.node_id,
            "layers": [self.start_layer, self.end_layer],
            "layer_count": self.end_layer - self.start_layer,
            "tensors": self.tensor_count,
            "resident_bytes": self.resident_bytes,
            # the accounting this number comes from, so a reader can check it
            "accounting": STAGE_ACCOUNTING_BASIS,
            "resident_bytes_by_dtype": dict(sorted(bytes_by_dtype.items())),
            "owns_embed": self.owns_embed,
            "owns_head": self.owns_head,
            "head_tied_to_embeddings": self.head_tied_to_embeddings,
            "head_tensor": self.head_tensor,
            "representation": self.representation,
            "kernels": self.kernels,
            "runtime_dtype": (
                "bfloat16" if self.kernels == KERNELS_TESTBED else "float32"
            ),
            "cacheless": self.cacheless,
            "shards": shard_records,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "num_hidden_layers": expected,
            "config_digest": "sha256:"
            + hashlib.sha256(
                json.dumps(self._config, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }

    # -- forward ------------------------------------------------------------ #

    def reset(self) -> None:
        self._kv.clear()
        self._sequence_seen = 0

    @property
    def kv_len(self) -> int:
        """Positions currently cached: the sequence axis, not the head axis.

        Always zero on the cacheless repo-kernel path, which keeps no cross-step
        state; there the reported figure is the sequence the stage last processed,
        so the counter still means "positions this placement has run forward".
        """

        if self._kv:
            return int(self._kv[self.start_layer][0].shape[0])
        return int(self._sequence_seen)

    def _rms(self, x: Any, weight: Any) -> Any:
        import mlx.core as mx

        eps = float(self._config.get("rms_norm_eps", 1e-6))
        widened = x.astype(mx.float32)
        mean_square = mx.mean(widened * widened, axis=-1, keepdims=True)
        normalised = widened * mx.rsqrt(mean_square + eps)
        return (normalised * weight.astype(mx.float32)).astype(x.dtype)

    def _rope(self, x: Any, positions: Any) -> Any:
        import mlx.core as mx

        head_dim = int(self._config["hidden_size"]) // int(
            self._config["num_attention_heads"]
        )
        theta = float(self._config.get("rope_theta", 1_000_000.0))
        half = head_dim // 2
        index = mx.arange(half, dtype=mx.float32)
        frequencies = 1.0 / (theta ** (index / half))
        angles = positions.astype(mx.float32)[:, None] * frequencies[None, :]
        cosines = mx.cos(angles)[:, None, :]
        sines = mx.sin(angles)[:, None, :]
        first = x[..., :half]
        second = x[..., half:]
        return mx.concatenate(
            [first * cosines - second * sines, first * sines + second * cosines],
            axis=-1,
        )

    def _layer(self, hidden: Any, index: int, positions: Any, mask: Any | None) -> Any:
        import mlx.core as mx

        prefix = f"model.layers.{index}."
        weights = self._weights
        heads = int(self._config["num_attention_heads"])
        kv_heads = int(self._config["num_key_value_heads"])
        head_dim = int(self._config["hidden_size"]) // heads

        normed = self._rms(hidden, weights[prefix + "input_layernorm.weight"])
        query = normed @ weights[prefix + "self_attn.q_proj.weight"].T
        key = normed @ weights[prefix + "self_attn.k_proj.weight"].T
        value = normed @ weights[prefix + "self_attn.v_proj.weight"].T
        for name, projected in (("q_proj", query), ("k_proj", key), ("v_proj", value)):
            bias = weights.get(prefix + f"self_attn.{name}.bias")
            if bias is not None:
                projected = projected + bias
            if name == "q_proj":
                query = projected
            elif name == "k_proj":
                key = projected
            else:
                value = projected

        sequence = int(hidden.shape[0])
        query = query.reshape(sequence, heads, head_dim)
        key = key.reshape(sequence, kv_heads, head_dim)
        value = value.reshape(sequence, kv_heads, head_dim)
        query = self._rope(query, positions)
        key = self._rope(key, positions)

        if index in self._kv:
            cached_key, cached_value = self._kv[index]
            key = mx.concatenate([cached_key, key], axis=0)
            value = mx.concatenate([cached_value, value], axis=0)
        self._kv[index] = (key, value)

        repeats = heads // kv_heads
        keys = mx.repeat(key, repeats, axis=1)
        values = mx.repeat(value, repeats, axis=1)
        # Per-head attention in the (heads, sequence, length) layout so the mask can
        # never be broadcast against a head axis by accident.
        scores = (query.transpose(1, 0, 2) @ keys.transpose(1, 2, 0)) / (head_dim**0.5)
        scores = scores.astype(mx.float32)
        if mask is not None:
            scores = scores + mask
        probabilities = mx.softmax(scores, axis=-1).astype(hidden.dtype)
        attended = probabilities @ values.transpose(1, 0, 2)
        attended = attended.transpose(1, 0, 2).reshape(sequence, heads * head_dim)
        attended = attended @ weights[prefix + "self_attn.o_proj.weight"].T
        hidden = hidden + attended

        normed = self._rms(hidden, weights[prefix + "post_attention_layernorm.weight"])
        gate = normed @ weights[prefix + "mlp.gate_proj.weight"].T
        up = normed @ weights[prefix + "mlp.up_proj.weight"].T
        activated = mx.sigmoid(gate) * gate * up
        down = activated @ weights[prefix + "mlp.down_proj.weight"].T
        return hidden + down

    def _run_range(self, hidden: Any, positions: Any, mask: Any | None) -> Any:
        for index in range(self.start_layer, self.end_layer):
            hidden = self._layer(hidden, index, positions, mask)
        return hidden

    def _mask_for(self, sequence: int, keys: int) -> Any | None:
        import mlx.core as mx

        if sequence == 1:
            return None
        total = keys - sequence
        rows = mx.arange(sequence)[:, None] + total
        columns = mx.arange(keys)[None, :]
        allowed = columns <= rows
        # (1, sequence, keys): the head axis is broadcast, never the mask's own.
        return mx.where(
            allowed,
            mx.zeros((sequence, keys), mx.float32),
            mx.full((sequence, keys), -1e9, mx.float32),
        )[None, :, :]

    def prefill(self, token_ids: Sequence[int]) -> bytes | int:
        """First pass over the prompt: returns hidden bytes, or a token id."""

        import mlx.core as mx

        tokens = mx.array(np.asarray(list(token_ids), dtype=np.int32))[None, :]
        hidden = self._weights["model.embed_tokens.weight"][tokens]
        hidden = hidden.reshape(len(token_ids), self.hidden_size)
        positions = mx.arange(len(token_ids), dtype=mx.float32)
        hidden = self._run_range(
            hidden, positions, self._mask_for(len(token_ids), len(token_ids))
        )
        return self._finish(hidden)

    def prefill_from_hidden(self, hidden: Any) -> bytes | int:
        """Continue a prefill whose embedding an earlier placement already built."""

        import mlx.core as mx

        sequence = int(hidden.shape[0])
        hidden = self._cast_to_stage_dtype(hidden)
        positions = mx.arange(sequence, dtype=mx.float32)
        hidden = self._run_range(hidden, positions, self._mask_for(sequence, sequence))
        return self._finish(hidden)

    def step(self, hidden_or_token: Any) -> bytes | int:
        """One decode step: advance the pipeline by exactly one position."""

        import mlx.core as mx

        if isinstance(hidden_or_token, int):
            tokens = mx.array(np.asarray([hidden_or_token], dtype=np.int32))[None, :]
            hidden = self._weights["model.embed_tokens.weight"][tokens]
            hidden = hidden.reshape(1, self.hidden_size)
        else:
            hidden = hidden_or_token
            if hidden.ndim == 2:
                hidden = hidden.reshape(1, self.hidden_size)
            hidden = self._cast_to_stage_dtype(hidden)
        position = self.kv_len
        positions = mx.array(np.asarray([position], dtype=np.float32))
        hidden = self._run_range(hidden, positions, None)
        return self._finish(hidden)

    def _cast_to_stage_dtype(self, hidden: Any) -> Any:
        """Make an inbound activation match the stage's own weight dtype.

        The pipeline carries activations as float32 on the wire; MLX will not
        matmul across dtypes, so the receiving stage casts to the dtype of its own
        weights (bfloat16 for this snapshot) rather than silently promoting every
        weight to float32 and doubling the resident footprint.
        """

        reference = self._weights[
            f"model.layers.{self.start_layer}.input_layernorm.weight"
        ]
        return hidden.astype(reference.dtype)

    @property
    def hidden_size(self) -> int:
        return int(self._config["hidden_size"])

    @property
    def vocab_size(self) -> int:
        return int(self._config["vocab_size"])

    def _finish(self, hidden: Any) -> bytes | int:
        import mlx.core as mx

        if not self.owns_head:
            # MLX bfloat16 does not convert straight to a numpy buffer (its
            # PEP-3118 format string is 2 bytes; numpy reads it as 1), so narrow to
            # float32 inside MLX first.  The wire format is then explicitly
            # little-endian float32 for both placements.
            widened = hidden.astype(mx.float32)
            mx.eval(widened)
            return np.asarray(widened, dtype=np.float32).tobytes()
        normed = self._rms(hidden, self._weights["model.norm.weight"])
        logits = normed @ self._weights["model.embed_tokens.weight"].T
        row = logits.reshape(-1, logits.shape[-1])[-1]
        mx.eval(row)
        # The product's own deterministic greedy decoder, imported read-only: the
        # same quantised-high bucket choice the router makes on every host.
        return int(quantized_greedy_token_id(row.tolist()))

    # -- the repository's own Qwen2 kernels ---------------------------------- #
    #
    # ``runtime_loader._qwen2_block`` builds its own causal mask over the whole
    # sequence and ``execute_loaded_stage`` documents the contract plainly: "There
    # is intentionally no KV-cache interface, so callers must pass the complete
    # sequence on every invocation."  These two entry points honour it - the entry
    # placement takes the running token sequence, the continuation placement takes
    # the whole hidden sequence - so every step is a real two-hop pipeline of real
    # activations through the repo kernel over the shaped relay path.

    @staticmethod
    def _runtime_loader() -> Any:
        import runtime_loader

        return runtime_loader

    def _repo_forward(self, hidden: Any, mx: Any) -> Any:
        loader = self._runtime_loader()
        for index in range(self.start_layer, self.end_layer):
            hidden = loader._qwen2_block(
                hidden,
                self._weights,
                f"model.layers.{index}.",
                self._repo_config,
                mx,
                "qwen2",
            )
        return hidden

    def _repo_finish(self, hidden: Any, mx: Any) -> Any:
        loader = self._runtime_loader()
        if not self.owns_head:
            widened = hidden.astype(mx.float32)
            mx.eval(widened)
            return np.asarray(widened, dtype=np.float32).tobytes()
        normed = loader._rms_norm(
            hidden,
            self._weights[FINAL_NORM_TENSOR],
            float(self._repo_config["rms_norm_epsilon"]),
            mx,
        )
        logits = loader._qwen2_linear(normed, self._weights[self.head_tensor], mx)
        row = logits.reshape(-1, logits.shape[-1])[-1]
        mx.eval(row)
        return int(quantized_greedy_token_id(row.tolist()))

    def run_entry(self, token_ids: Sequence[int]) -> bytes | int:
        """The entry placement over the full running sequence (repo kernels)."""

        import mlx.core as mx

        tokens = mx.array(np.asarray(list(token_ids), dtype=np.int32))[None, :]
        self._sequence_seen = len(token_ids)
        hidden = self._runtime_loader()._qwen2_embedding(
            self._weights[EMBED_TOKENS_TENSOR], tokens, mx
        )
        return self._repo_finish(self._repo_forward(hidden, mx), mx)

    def run_continuation(self, hidden: Any) -> bytes | int:
        """The continuation placement over the full hidden sequence (repo kernels)."""

        import mlx.core as mx

        array = hidden if isinstance(hidden, mx.array) else mx.array(hidden)
        if array.ndim == 2:
            array = array[None, :, :]
        array = array.astype(mx.float32)
        self._sequence_seen = int(array.shape[1])
        return self._repo_finish(self._repo_forward(array, mx), mx)


# --------------------------------------------------------------------------- #
# wire framing between the driver and a worker
# --------------------------------------------------------------------------- #

_FRAME_LENGTH = __import__("struct").Struct(">I")


def encode_frame(header: Mapping[str, Any], payload: bytes = b"") -> bytes:
    document = dict(header)
    document["payload_bytes"] = len(payload)
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _FRAME_LENGTH.pack(len(raw)) + raw + payload


async def read_frame(
    reader: asyncio.StreamReader,
) -> tuple[dict[str, Any], bytes]:
    prefix = await reader.readexactly(_FRAME_LENGTH.size)
    (length,) = _FRAME_LENGTH.unpack(prefix)
    header = json.loads((await reader.readexactly(length)).decode("utf-8"))
    size = int(header.get("payload_bytes", 0))
    payload = await reader.readexactly(size) if size else b""
    return header, payload


def frame_digest(header: Mapping[str, Any], payload: bytes = b"") -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(dict(header), sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    digest.update(b"\0")
    digest.update(payload)
    return "sha256:" + digest.hexdigest()


# --------------------------------------------------------------------------- #
# the worker process
# --------------------------------------------------------------------------- #


class WorkerState:
    """Everything one worker process owns: its home, identity, stage and counters."""

    def __init__(
        self,
        *,
        node_id: str,
        home: Path,
        model_root: Path,
        start_layer: int,
        end_layer: int,
        owns_embed: bool,
        owns_head: bool,
        cleanup_latency_ms: float,
        run_dir: Path | None,
        monotonic_offset_ns: int,
        wall_offset_ns: int,
        placement_id: str,
        model_id: str = MODEL_ID,
        model_revision: str = MODEL_REVISION,
        representation: str = REPRESENTATION_SOURCE,
        kernels: str = KERNELS_TESTBED,
        expected_layers: int | None = None,
        sharded: bool = False,
    ) -> None:
        self.node_id = node_id
        self.home = home
        self.placement_id = placement_id
        self.identity = establish_identity(home, node_id)
        self.stage = QwenStage(
            model_root=model_root,
            node_id=node_id,
            start_layer=start_layer,
            end_layer=end_layer,
            owns_embed=owns_embed,
            owns_head=owns_head,
            model_id=model_id,
            model_revision=model_revision,
            representation=representation,
            kernels=kernels,
            expected_layers=expected_layers,
            sharded=sharded,
        )
        self.stage_document = self.stage.load()
        self.cleanup_latency_ms = cleanup_latency_ms
        self.clock = HostClock(
            monotonic_offset_ns=monotonic_offset_ns, wall_offset_ns=wall_offset_ns
        )
        self.run_dir = run_dir
        self.recorder: TraceRecorder | None = None
        self.counters: dict[str, Any] = {
            "node_id": node_id,
            "placement_id": placement_id,
            "commands": 0,
            "layers_executed": 0,
            "tokens_processed": 0,
            "hidden_bytes_in": 0,
            "hidden_bytes_out": 0,
            "model_ms_total": 0.0,
            "kv_len": 0,
            "cleanups": 0,
        }
        if run_dir is not None:
            run = TraceRunDirectory.attach(run_dir)
            self.recorder = TraceRecorder(
                node_id,
                run=run,
                clock=self.clock,
                role=f"emulation_worker:layers[{start_layer},{end_layer})",
            )

    def trace(
        self,
        kind: EventKind | str,
        *,
        request_id: str | None = None,
        command_id: str | None = None,
        status: str = "ok",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        if self.recorder is None:
            return
        self.recorder.event(
            kind,
            request_id=request_id,
            command_id=command_id,
            placement_id=self.placement_id,
            status=status,
            detail=detail,
        )

    def stamp(self) -> tuple[int, int]:
        moment = self.clock.stamp()
        return moment.monotonic_ns, moment.wall_ns

    def dispatch(
        self, header: Mapping[str, Any], payload: bytes
    ) -> tuple[dict[str, Any], bytes]:
        operation = header.get("op")
        if operation == "hello":
            return (
                {
                    "op": "hello",
                    "ok": True,
                    "node_id": self.node_id,
                    "placement_id": self.placement_id,
                    "identity": self.identity.to_document(),
                    "public_key_pem": self.identity.public_key_pem,
                    "stage": self.stage_document,
                    "pid": os.getpid(),
                    "home": str(self.home),
                },
                b"",
            )
        if operation == "ping":
            receive = self.stamp()
            send = self.stamp()
            return (
                {
                    "op": "ping",
                    "ok": True,
                    "node_id": self.node_id,
                    "worker_recv_monotonic_ns": receive[0],
                    "worker_recv_wall_ns": receive[1],
                    "worker_send_monotonic_ns": send[0],
                    "worker_send_wall_ns": send[1],
                },
                b"",
            )
        if operation == "layers":
            return self._layers(header, payload)
        if operation == "cleanup":
            return self._cleanup(header)
        if operation == "counters":
            counters = dict(self.counters)
            # The worker's own high-water mark, reported beside the driver's external
            # sampler rather than instead of it.  The sampler only starts once both
            # placements have finished loading, so on a model this size it misses the
            # load peak; ``ru_maxrss`` is a lifetime high-water mark and therefore
            # includes it.  A self-report is not a verdict - the deterministic checks
            # stay on the stage's own load accounting - so it is labelled as one.
            counters["self_reported_peak_rss_bytes"], counters[
                "self_reported_peak_rss_unit"
            ] = _self_reported_peak_rss()
            return (
                {"op": "counters", "ok": True, "counters": counters},
                b"",
            )
        return (
            {"op": operation, "ok": False, "error": f"unknown_operation:{operation}"},
            b"",
        )

    def _layers(
        self, header: Mapping[str, Any], payload: bytes
    ) -> tuple[dict[str, Any], bytes]:
        request_id = header.get("request_id")
        command_id = header.get("command_id")
        phase = header.get("phase")
        token_index = int(header.get("token_index", 0))
        self.counters["commands"] += 1
        self.trace(
            EventKind.DELIVERED,
            request_id=request_id,
            command_id=command_id,
            detail={
                "phase": phase,
                "token_index": token_index,
                "frame_bytes": len(payload),
                "relay_shaped": True,
                "origin": "worker.recv",
            },
        )
        self.trace(
            EventKind.STARTED,
            request_id=request_id,
            command_id=command_id,
            detail={
                "phase": phase,
                "token_index": token_index,
                "layers": [self.stage.start_layer, self.stage.end_layer],
                "origin": "worker.execute",
            },
        )
        started = time.perf_counter()
        try:
            if self.stage.cacheless:
                # The repo kernel's contract: the whole sequence every time, no
                # cross-step state, so the entry placement is driven by the running
                # token sequence and the continuation by the running hidden sequence.
                if self.stage.owns_embed:
                    output = self.stage.run_entry(list(header["token_ids"]))
                else:
                    import mlx.core as mx

                    hidden = np.frombuffer(payload, dtype=np.float32).reshape(
                        int(header["sequence"]), int(header["hidden"])
                    )
                    output = self.stage.run_continuation(mx.array(hidden))
            elif phase == "prefill":
                if not self.stage.owns_embed:
                    hidden = np.frombuffer(payload, dtype=np.float32).reshape(
                        int(header["sequence"]), int(header["hidden"])
                    )
                    import mlx.core as mx

                    output = self.stage.prefill_from_hidden(mx.array(hidden))
                else:
                    output = self.stage.prefill(list(header["token_ids"]))
            else:
                if self.stage.owns_embed:
                    output = self.stage.step(int(header["token_id"]))
                else:
                    import mlx.core as mx

                    hidden = np.frombuffer(payload, dtype=np.float32).reshape(
                        1, int(header["hidden"])
                    )
                    output = self.stage.step(mx.array(hidden))
        except BaseException as error:  # noqa: BLE001 - reported, then re-raised
            self.trace(
                EventKind.FINISHED,
                request_id=request_id,
                command_id=command_id,
                status=f"failed:{type(error).__name__}",
                detail={"error": str(error)[:200], "origin": "worker.execute"},
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.counters["layers_executed"] += (
            self.stage.end_layer - self.stage.start_layer
        )
        self.counters["model_ms_total"] += elapsed_ms
        self.counters["kv_len"] = self.stage.kv_len
        reply_header: dict[str, Any] = {
            "op": "layers",
            "ok": True,
            "node_id": self.node_id,
            "placement_id": self.placement_id,
            "phase": phase,
            "token_index": token_index,
            "layers": [self.stage.start_layer, self.stage.end_layer],
            "layers_executed": self.stage.end_layer - self.stage.start_layer,
            "kv_len": self.stage.kv_len,
            "model_ms": round(elapsed_ms, 4),
        }
        reply_payload = b""
        if isinstance(output, int):
            self.counters["tokens_processed"] += 1
            self.counters["hidden_bytes_out"] += 0
            reply_header["token_id"] = output
            reply_header["output"] = "token_id"
        else:
            self.counters["hidden_bytes_out"] += len(output)
            self.counters["hidden_bytes_in"] += len(payload)
            reply_header["hidden"] = self.stage.hidden_size
            reply_header["output"] = "hidden"
            reply_payload = output
        self.trace(
            EventKind.FINISHED,
            request_id=request_id,
            command_id=command_id,
            detail={
                "phase": phase,
                "token_index": token_index,
                "model_ms": reply_header["model_ms"],
                "kv_len": reply_header["kv_len"],
                "output": reply_header["output"],
                "token_id": reply_header.get("token_id"),
                "counters": dict(self.counters),
                "origin": "worker.execute",
            },
        )
        return reply_header, reply_payload

    def _cleanup(self, header: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
        request_id = header.get("request_id")
        command_id = header.get("command_id")
        latency_ms = float(header.get("cleanup_latency_ms", self.cleanup_latency_ms))
        self.trace(
            EventKind.STARTED,
            request_id=request_id,
            command_id=command_id,
            detail={
                "operation": "cleanup",
                "cleanup_latency_ms": latency_ms,
                "origin": "worker.cleanup",
            },
        )
        started = time.perf_counter()
        # The node's own teardown grid (physical_inference_node.py:74, 0.05 s) and
        # its real teardown work, modelled as one bounded sleep: the point of the
        # negative case is that the *receipt* is late, not that the node is slow.
        time.sleep(max(0.0, latency_ms) / 1000.0)
        self.stage.reset()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.counters["cleanups"] += 1
        moment = self.stamp()
        receipt = {
            "request_id": request_id,
            "command_id": command_id,
            "node_id": self.node_id,
            "placement_id": self.placement_id,
            "cleanup_ms": round(elapsed_ms, 4),
            "kv_released": True,
            "monotonic_ns": moment[0],
            "wall_ns": moment[1],
        }
        receipt["receipt_digest"] = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
        )
        self.trace(
            EventKind.FINISHED,
            request_id=request_id,
            command_id=command_id,
            detail={
                "operation": "cleanup",
                "cleanup_ms": receipt["cleanup_ms"],
                "receipt_digest": receipt["receipt_digest"],
                "kv_released": True,
                "origin": "worker.cleanup",
            },
        )
        return ({"op": "cleanup", "ok": True, "receipt": receipt}, b"")


def worker_main(argv: Sequence[str] | None = None) -> int:
    """Entry point for one worker process (separate home, separate identity)."""

    parser = argparse.ArgumentParser(description="mycelium testbed emulation worker")
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--placement-id", required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--start-layer", type=int, required=True)
    parser.add_argument("--end-layer", type=int, required=True)
    parser.add_argument("--owns-embed", action="store_true")
    parser.add_argument("--owns-head", action="store_true")
    parser.add_argument("--cleanup-latency-ms", type=float, default=60.0)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--monotonic-offset-ns", type=int, default=0)
    parser.add_argument("--wall-offset-ns", type=int, default=0)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--model-id", default=MODEL_ID)
    parser.add_argument("--model-revision", default=MODEL_REVISION)
    parser.add_argument("--representation", default=REPRESENTATION_SOURCE)
    parser.add_argument("--kernels", default=KERNELS_TESTBED)
    parser.add_argument("--expected-layers", type=int, default=None)
    parser.add_argument("--sharded", action="store_true")
    args = parser.parse_args(argv)

    state = WorkerState(
        node_id=args.node_id,
        home=args.home,
        model_root=args.model_root,
        start_layer=args.start_layer,
        end_layer=args.end_layer,
        owns_embed=args.owns_embed,
        owns_head=args.owns_head,
        cleanup_latency_ms=args.cleanup_latency_ms,
        run_dir=args.run_dir,
        monotonic_offset_ns=args.monotonic_offset_ns,
        wall_offset_ns=args.wall_offset_ns,
        placement_id=args.placement_id,
        model_id=args.model_id,
        model_revision=args.model_revision,
        representation=args.representation,
        kernels=args.kernels,
        expected_layers=args.expected_layers,
        sharded=args.sharded,
    )

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    header, payload = await read_frame(reader)
                except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
                    return
                try:
                    reply_header, reply_payload = state.dispatch(header, payload)
                except BaseException as error:  # noqa: BLE001
                    # A worker that closes the socket without a word is the worst
                    # possible failure mode: the driver sees an empty read and has
                    # nothing to report.  Print the real traceback and answer with
                    # an error frame instead.
                    traceback.print_exc()
                    reply_header = {
                        "op": header.get("op"),
                        "ok": False,
                        "node_id": state.node_id,
                        "error": f"{type(error).__name__}:{error}"[:500],
                    }
                    reply_payload = b""
                writer.write(encode_frame(reply_header, reply_payload))
                await writer.drain()
        finally:
            try:
                writer.close()
            except OSError:
                pass

    async def serve() -> int:
        server = await asyncio.start_server(handle, args.listen_host, 0)
        port = int(server.sockets[0].getsockname()[1])
        print(
            json.dumps(
                {
                    "event": "listening",
                    "node_id": state.node_id,
                    "placement_id": state.placement_id,
                    "pid": os.getpid(),
                    "home": str(args.home),
                    "port": port,
                    "identity": state.identity.to_document(),
                    "stage": state.stage_document,
                    "counters": dict(state.counters),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        async with server:
            await server.serve_forever()
        return 0

    try:
        return asyncio.run(serve())
    except KeyboardInterrupt:
        return 0


# --------------------------------------------------------------------------- #
# the driver: route + gateway over two placements
# --------------------------------------------------------------------------- #


@dataclass
class WorkerRuntime:
    """The driver's view of one worker process."""

    node_id: str
    placement_id: str
    home: Path
    process: subprocess.Popen[str]
    log_path: Path
    port: int = 0
    identity: dict[str, Any] = field(default_factory=dict)
    stage: dict[str, Any] = field(default_factory=dict)
    relay: FaultProxyHandle | None = None
    counters: dict[str, Any] = field(default_factory=dict)
    peak_rss_bytes: int = 0
    rss_samples: int = 0

    def to_document(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "placement_id": self.placement_id,
            "home": str(self.home),
            "pid": self.process.pid,
            "listen_port": self.port,
            "identity": self.identity,
            "stage": self.stage,
            "relay": self.relay.document() if self.relay is not None else None,
            "counters": self.counters,
            # observed by the driver with ``ps`` while the requests ran, so the
            # memory envelope is a measurement rather than an assertion
            "peak_rss_bytes": self.peak_rss_bytes,
            "rss_samples": self.rss_samples,
        }


@dataclass
class CleanupOutcome:
    """One request's cleanup exchange, with the route-side poll grid measured."""

    request_id: str
    receipts: dict[str, dict[str, Any]]
    missing: tuple[str, ...]
    status: str
    observation_delay_ms: float
    poll_waits_ms: list[float]
    waited_ms: float
    window_ms: float
    budget_ms: float

    def to_document(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "missing": list(self.missing),
            "receipts": self.receipts,
            "waited_ms": round(self.waited_ms, 4),
            "window_ms": self.window_ms,
            "budget_ms": self.budget_ms,
            # The route's observation grid: how long after the last receipt was
            # sealed the bounded cadence poll actually noticed it.
            "observation_delay_ms": round(self.observation_delay_ms, 4),
            "poll_waits": len(self.poll_waits_ms),
            "poll_waits_max_ms": round(
                max(self.poll_waits_ms) if self.poll_waits_ms else 0.0, 4
            ),
        }


@dataclass
class EmulationResult:
    """Everything one emulation run produced."""

    run_dir: Path
    document: dict[str, Any]
    summary: str
    tokens: tuple[int, ...]
    generated_text: str
    receipts: dict[str, dict[str, Any]]
    placement_work: tuple[dict[str, Any], ...]
    negative_terminal_status: str
    negative_elapsed_ms: float
    negative_receipts_missing: tuple[str, ...]
    d9_reason: str | None
    events: tuple[Any, ...]

    @property
    def terminal_reached(self) -> bool:
        return self.document["status"]["ok_terminal"] == "completed"

    @property
    def negative_case_is_named_failure(self) -> bool:
        status = self.negative_terminal_status
        return status.startswith(CLEANUP_RECEIPT_TIMEOUT) and bool(
            status.split("|", 1)[1].strip()
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "run_dir": str(self.run_dir),
            "terminal_reached": self.terminal_reached,
            "negative_case_named_failure": self.negative_case_is_named_failure,
            "negative_terminal_status": self.negative_terminal_status,
            "negative_elapsed_ms": round(self.negative_elapsed_ms, 4),
            "negative_receipts_missing": list(self.negative_receipts_missing),
            "d9_reason": self.d9_reason,
            "tokens": list(self.tokens),
            "generated_text": self.generated_text,
            "receipts": self.receipts,
            "placement_work": list(self.placement_work),
            **self.document,
        }


def clean_subprocess_environment() -> dict[str, str]:
    """The acceptance command's environment rule, applied to the children too."""

    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP"):
        environment.pop(key, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _self_reported_peak_rss() -> tuple[int, str]:
    """``(value, unit)`` for this process's own peak RSS high-water mark."""

    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value, "bytes" if sys.platform == "darwin" else "kibibytes"


def _host_available_bytes() -> int | None:
    """``hw.memsize`` on this host, read only, or ``None`` when unavailable."""

    try:
        completed = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = completed.stdout.strip()
    return int(raw) if raw.isdigit() else None


def _read_rss_bytes(pid: int) -> int | None:
    """Resident set size of one process, in bytes, or ``None`` if it is gone."""

    try:
        completed = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = completed.stdout.strip()
    if not raw.isdigit():
        return None
    return int(raw) * 1024  # ps reports kibibytes


# --------------------------------------------------------------------------- #
# the memory envelope: what the stage accounts for, beside what the OS reported
# --------------------------------------------------------------------------- #


def read_safetensors_tensor_bytes(path: Path) -> dict[str, int]:
    """Bytes per tensor, read from a safetensors *header* only.

    No tensor is loaded and no RSS is consulted: this is the file's own metadata, so
    it is the deterministic side of the memory check - the checkpoint's statement of
    what the placement's tensors are, against the stage's accounting of what it put
    in memory.
    """

    weights = Path(path)
    with weights.open("rb") as handle:
        prefix = handle.read(8)
        if len(prefix) != 8:
            raise EmulationError(f"safetensors_header_truncated:{weights}")
        header_bytes = int.from_bytes(prefix, "little")
        raw = handle.read(header_bytes)
        if len(raw) != header_bytes:
            raise EmulationError(f"safetensors_header_truncated:{weights}")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EmulationError(f"safetensors_header_unreadable:{weights}") from error
    if not isinstance(header, dict):
        raise EmulationError(f"safetensors_header_not_an_object:{weights}")
    sizes: dict[str, int] = {}
    for name, entry in header.items():
        if name == "__metadata__":
            continue
        offsets = entry.get("data_offsets") if isinstance(entry, dict) else None
        if not (isinstance(offsets, list) and len(offsets) == 2):
            raise EmulationError(f"safetensors_header_entry_invalid:{weights}:{name}")
        sizes[str(name)] = int(offsets[1]) - int(offsets[0])
    return sizes


def read_safetensors_tensor_meta(path: Path) -> dict[str, dict[str, Any]]:
    """Per-tensor ``{bytes, dtype, shape}``, read from a safetensors header only.

    No tensor is loaded and no RSS is consulted: this is the file's own metadata.
    """

    weights = Path(path)
    with weights.open("rb") as handle:
        prefix = handle.read(8)
        if len(prefix) != 8:
            raise EmulationError(f"safetensors_header_truncated:{weights}")
        header_bytes = int.from_bytes(prefix, "little")
        raw = handle.read(header_bytes)
        if len(raw) != header_bytes:
            raise EmulationError(f"safetensors_header_truncated:{weights}")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EmulationError(f"safetensors_header_unreadable:{weights}") from error
    if not isinstance(header, dict):
        raise EmulationError(f"safetensors_header_not_an_object:{weights}")
    meta: dict[str, dict[str, Any]] = {}
    for name, entry in header.items():
        if name == "__metadata__":
            continue
        offsets = entry.get("data_offsets") if isinstance(entry, dict) else None
        shape = entry.get("shape") if isinstance(entry, dict) else None
        if not (isinstance(offsets, list) and len(offsets) == 2):
            raise EmulationError(f"safetensors_header_entry_invalid:{weights}:{name}")
        if not (isinstance(shape, list) and shape):
            raise EmulationError(f"safetensors_header_shape_invalid:{weights}:{name}")
        meta[str(name)] = {
            "bytes": int(offsets[1]) - int(offsets[0]),
            "dtype": str(entry.get("dtype")),
            "shape": [int(value) for value in shape],
        }
    return meta


def snapshot_memory_facts(
    model_root: Path,
    *,
    representation: str = REPRESENTATION_SOURCE,
    sharded: bool = False,
) -> tuple[dict[str, Any], dict[str, int]]:
    """``(document facts, bytes per tensor in ``representation``)`` for the snapshot.

    For the source dtype the bytes are the header's own ``data_offsets``; for the
    repository's int8 representation they are the codec's arithmetic over the same
    header (int8 code per element + one float32 scale per output row), so the
    number the stage accounts for can be checked against the checkpoint without
    either side consulting RSS.
    """

    root = Path(model_root)
    if sharded:
        index_path = root / "model.safetensors.index.json"
        if not index_path.is_file():
            raise EmulationError(f"safetensors_index_missing:{index_path}")
        weight_map = json.loads(index_path.read_text(encoding="utf-8"))["weight_map"]
        shards = sorted(set(weight_map.values()))
        meta: dict[str, dict[str, Any]] = {}
        payload = 0
        for shard in shards:
            path = root / shard
            payload += path.stat().st_size
            meta.update(read_safetensors_tensor_meta(path))
        sizes = {
            name: _representation_tensor_bytes(
                entry["dtype"], entry["shape"], name, representation
            )
            for name, entry in meta.items()
        }
        tied = bool(
            json.loads((root / "config.json").read_text(encoding="utf-8")).get(
                "tie_word_embeddings", False
            )
        )
        facts: dict[str, Any] = {
            "path": str(index_path),
            "shards": [str(root / shard) for shard in shards],
            "shard_count": len(shards),
            "payload_bytes": payload,
            "source": (
                "safetensors shard headers via the snapshot's model.safetensors."
                "index.json (file metadata only; no tensors read, no RSS involved)"
            ),
            "tensors": len(meta),
            "representation": representation,
            "representation_basis": (
                "int8 code per element plus one float32 scale per output row for "
                "every 2-D .weight, float32 for the rest (mycelium.rowwise_"
                "symmetric_int8.v1); source dtype elsewhere"
            ),
            "source_payload_bytes": sum(entry["bytes"] for entry in meta.values()),
            "tie_word_embeddings": tied,
            "tied_embedding_bytes": sizes.get(EMBED_TOKENS_TENSOR, 0) if tied else 0,
            "tied_embedding_tensor": EMBED_TOKENS_TENSOR,
        }
        return facts, sizes

    weights = root / "model.safetensors"
    sizes = read_safetensors_tensor_bytes(weights)
    facts = {
        "path": str(weights),
        "payload_bytes": sum(sizes.values()),
        "source": (
            "model.safetensors header data_offsets (file metadata only; no tensors "
            "read, no RSS involved)"
        ),
        "tensors": len(sizes),
        "tied_embedding_bytes": sizes.get(EMBED_TOKENS_TENSOR, 0),
        "tied_embedding_tensor": EMBED_TOKENS_TENSOR,
    }
    return facts, sizes


def placement_expected_bytes(
    tensor_bytes: Mapping[str, int],
    *,
    start_layer: int,
    end_layer: int,
    owns_embed: bool,
    owns_head: bool,
    head_tied_to_embeddings: bool,
    head_tensor: str | None = None,
) -> tuple[int, int]:
    """``(tensors, bytes)`` a placement must hold, per the snapshot's own header.

    The selection mirrors ``QwenStage.load`` against the file's metadata, which keeps
    the two accountings independent: the stage counts what it loaded, this counts
    what the checkpoint says those tensors are.  An untied snapshot's head is a
    tensor of its own (``lm_head.weight``), so it is named rather than assumed.
    """

    names: set[str] = set()
    for index in range(start_layer, end_layer):
        prefix = f"model.layers.{index}."
        names.update(name for name in tensor_bytes if name.startswith(prefix))
    if owns_embed or (owns_head and head_tied_to_embeddings):
        # Qwen2.5-0.5B ties its embeddings, so the placement that owns the head
        # legitimately holds the embedding matrix as the output projection too.
        if EMBED_TOKENS_TENSOR not in tensor_bytes:
            raise EmulationError(f"snapshot_missing_tensor:{EMBED_TOKENS_TENSOR}")
        names.add(EMBED_TOKENS_TENSOR)
    if owns_head:
        if FINAL_NORM_TENSOR not in tensor_bytes:
            raise EmulationError(f"snapshot_missing_tensor:{FINAL_NORM_TENSOR}")
        names.add(FINAL_NORM_TENSOR)
        if not head_tied_to_embeddings:
            key = head_tensor or LM_HEAD_TENSOR
            if key not in tensor_bytes:
                raise EmulationError(f"snapshot_missing_tensor:{key}")
            names.add(key)
    return len(names), sum(tensor_bytes[name] for name in sorted(names))


@dataclass(frozen=True)
class WorkerMemoryVerdict:
    """One placement's memory envelope: the accounting, and the observation beside it.

    The two are kept apart on purpose.  ``accounted_*`` is the stage's own load
    accounting and is deterministic; ``observed_*`` is a sampled OS quantity that a
    loaded machine may honestly under-report.  Only the accounting can produce a
    violation, so a machine's load cannot decide whether the suite is green.
    """

    node_id: str
    accounted_resident_bytes: int
    accounted_tensors: int
    accounted_bytes_by_dtype: dict[str, int]
    accounting_basis: str
    expected_resident_bytes: int | None
    expected_tensors: int | None
    expectation_source: str | None
    deterministic_checks: tuple[str, ...]
    deterministic_violations: tuple[str, ...]
    observed_peak_rss_bytes: int
    rss_samples: int
    sampling_method: str
    reclaim_tolerance: float
    rss_sanity_bound_bytes: int
    rss_sanity_satisfied: bool
    load_sensitive_checks: tuple[str, ...] = LOAD_SENSITIVE_CHECKS

    @property
    def deterministic_ok(self) -> bool:
        return not self.deterministic_violations

    def to_document(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "accounted": {
                "basis": self.accounting_basis,
                "bytes_by_dtype": dict(self.accounted_bytes_by_dtype),
                "deterministic": True,
                "resident_bytes": self.accounted_resident_bytes,
                "tensors": self.accounted_tensors,
            },
            "deterministic_checks": list(self.deterministic_checks),
            "deterministic_violations": list(self.deterministic_violations),
            "expected_from_snapshot": (
                None
                if self.expected_resident_bytes is None
                else {
                    "resident_bytes": self.expected_resident_bytes,
                    "source": self.expectation_source,
                    "tensors": self.expected_tensors,
                }
            ),
            "load_sensitive_checks": list(self.load_sensitive_checks),
            "note": (
                "accounted_* is the stage's own load accounting (deterministic, the "
                "only basis for a violation); the RSS figures are a sampled "
                "observation of the OS, recorded with their method and never a "
                "verdict - under memory pressure a reclaim can land them below the "
                "bytes the stage loaded, which says nothing about the stage"
            ),
            "observation": {
                "load_sensitive": True,
                "method": self.sampling_method,
                "peak_rss_bytes": self.observed_peak_rss_bytes,
                "quantity": "peak_resident_set_size_sampled",
                "samples": self.rss_samples,
            },
            "rss_sanity": {
                "bound_bytes": self.rss_sanity_bound_bytes,
                "observed_over_accounted": (
                    round(self.observed_peak_rss_bytes / self.accounted_resident_bytes, 4)
                    if self.accounted_resident_bytes
                    else None
                ),
                "reclaim_tolerance": self.reclaim_tolerance,
                "role": "recorded only: never counted as a violation",
                "satisfied": self.rss_sanity_satisfied,
            },
        }


def evaluate_worker_memory_envelope(worker: Mapping[str, Any]) -> WorkerMemoryVerdict:
    """Judge one worker's memory envelope from what its stage accounted for.

    Every deterministic check reads the stage's own load accounting and, when the
    driver recorded it, the snapshot header the placement was expected to cover.  The
    sampled peak RSS is carried into the verdict as an observation with its sampling
    method and its stated reclaim tolerance, and is deliberately excluded from
    ``deterministic_violations``.
    """

    node_id = str(worker.get("node_id") or "unknown")
    stage = worker.get("stage") or {}
    accounted_resident_bytes = int(stage.get("resident_bytes") or 0)
    accounted_tensors = int(stage.get("tensors") or 0)
    bytes_by_dtype = {
        str(dtype): int(value)
        for dtype, value in dict(stage.get("resident_bytes_by_dtype") or {}).items()
    }
    expectation = dict(worker.get("placement_expectation") or {})
    expected_bytes = expectation.get("resident_bytes")
    expected_tensors = expectation.get("tensors")
    expectation_source = expectation.get("source")

    checks: list[str] = ["accounted_resident_bytes_positive"]
    violations: list[str] = []
    if accounted_resident_bytes <= 0:
        violations.append(f"accounted_resident_bytes_not_positive:{node_id}")

    if bytes_by_dtype:
        checks.append("resident_bytes_equal_tensor_byte_sum")
        if sum(bytes_by_dtype.values()) != accounted_resident_bytes:
            violations.append(f"resident_bytes_do_not_match_tensor_byte_sum:{node_id}")

    if isinstance(expected_bytes, int) and isinstance(expected_tensors, int):
        checks.append("accounted_bytes_match_snapshot_header")
        if accounted_resident_bytes != expected_bytes:
            violations.append(
                "accounted_bytes_do_not_match_snapshot_header:"
                f"{node_id}:{accounted_resident_bytes}!={expected_bytes}"
            )
        checks.append("accounted_tensors_match_snapshot_header")
        if accounted_tensors != expected_tensors:
            violations.append(
                "accounted_tensors_do_not_match_snapshot_header:"
                f"{node_id}:{accounted_tensors}!={expected_tensors}"
            )

    observed_peak_rss_bytes = int(worker.get("peak_rss_bytes") or 0)
    rss_sanity_bound_bytes = int(
        accounted_resident_bytes * (1.0 - RSS_RECLAIM_TOLERANCE)
    )
    return WorkerMemoryVerdict(
        node_id=node_id,
        accounted_resident_bytes=accounted_resident_bytes,
        accounted_tensors=accounted_tensors,
        accounted_bytes_by_dtype=bytes_by_dtype,
        accounting_basis=str(stage.get("accounting") or STAGE_ACCOUNTING_BASIS),
        expected_resident_bytes=(
            expected_bytes if isinstance(expected_bytes, int) else None
        ),
        expected_tensors=expected_tensors if isinstance(expected_tensors, int) else None,
        expectation_source=str(expectation_source) if expectation_source else None,
        deterministic_checks=tuple(checks),
        deterministic_violations=tuple(violations),
        observed_peak_rss_bytes=observed_peak_rss_bytes,
        rss_samples=int(worker.get("rss_samples") or 0),
        sampling_method=RSS_SAMPLING_METHOD,
        reclaim_tolerance=RSS_RECLAIM_TOLERANCE,
        rss_sanity_bound_bytes=rss_sanity_bound_bytes,
        rss_sanity_satisfied=observed_peak_rss_bytes >= rss_sanity_bound_bytes,
        load_sensitive_checks=LOAD_SENSITIVE_CHECKS,
    )


class Emulation:
    """The driver: gateway + route + fanout + terminal frame, over two workers."""

    def __init__(
        self,
        *,
        run_dir: Path,
        model_root: Path,
        seed: int,
        receipt_hold_ms: float | None = None,
        python: str | None = None,
        fsync: bool = True,
        token_steps: int = TOKEN_STEPS,
        profile: ModelProfile | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.model_root = Path(model_root)
        self.seed = int(seed)
        self.receipt_hold_ms = receipt_hold_ms
        self.python = python or sys.executable
        self.fsync = fsync
        self.token_steps = int(token_steps)
        self.profile = profile or pilot_profile()
        if tuple(self.profile.node_ids) != WORKER_HOSTS:
            raise EmulationError(
                f"profile_node_ids_not_supported:{self.profile.node_ids}"
            )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.homes_dir = self.run_dir / "homes"
        self.tls_dir = self.run_dir / "tls"
        self.logs_dir = self.run_dir / "logs"
        for directory in (self.homes_dir, self.tls_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)
        # NOTE: the receipt hold is NOT pre-armed here.  It is armed at run time for
        # exactly one cleanup exchange (FaultProxyHandle.set_hold), so the shaped
        # legs used by the successful request stay unheld; the negative case arms it
        # and the relay's own `held` counter is the proof that it fired.
        self.legs: tuple[Round50LegConfig, ...] = round50_legs(
            self.seed, receipt_hold_ms=None
        )
        self.origin: SseOrigin | None = None
        self.origin_relay: FaultProxyHandle | None = None
        self.workers: dict[str, WorkerRuntime] = {}
        self.driver_recorder: TraceRecorder | None = None
        self.run: TraceRunDirectory | None = None
        self.tokenizer: Any = None
        self._receipts: dict[str, dict[str, dict[str, Any]]] = {}
        self._sealed_at: dict[str, dict[str, float]] = {}
        self.document: dict[str, Any] = {}
        self.offsets: dict[str, Any] = {}
        self._sampling = False

    # -- setup -------------------------------------------------------------- #

    def _leg(self, name: str) -> Round50LegConfig:
        for leg in self.legs:
            if leg.name == name:
                return leg
        raise EmulationError(f"no_such_leg:{name}")

    @property
    def entry_cacheless(self) -> bool:
        """Whether the entry placement's kernel needs the whole sequence each step."""

        entry = self.workers.get(WORKER_HOSTS[0])
        return bool((entry.stage if entry is not None else {}).get("cacheless"))

    def entry_sequence_argument(
        self, prompt_ids: Sequence[int], generated: Sequence[int]
    ) -> dict[str, Any]:
        """The running token sequence, only for a cacheless entry placement.

        The pilot path's stage keeps a KV cache and needs just the new token, so
        its frames are unchanged; the repo-kernel path has no cache and is driven
        with the full sequence, as ``runtime_loader.execute_loaded_stage`` requires.
        """

        if not self.entry_cacheless:
            return {}
        sequence = [int(value) for value in prompt_ids] + [
            int(value) for value in generated
        ]
        return {"token_ids": sequence, "sequence": len(sequence)}

    def open_run(self) -> TraceRunDirectory:
        product_roots = (
            "mycelium_router",
            "mycelium_request_gateway",
            "mycelium_iroh_sidecar",
        )
        self.run = TraceRunDirectory(
            self.run_dir,
            run_id=self.run_dir.name,
            host_id=DRIVER_HOST,
            reference_host=DRIVER_HOST,
            fsync=self.fsync,
            product_package_digest=_product_digest(_REPO_ROOT, product_roots),
            product_roots=tuple(str(_REPO_ROOT / name) for name in product_roots),
            notes=(
                "PLAN 5.2 item 3 single-host two-worker C4->C5 emulation: two worker "
                "processes with separate homes and identities, the real "
                f"{self.profile.model_id}@{self.profile.revision} from disk in the "
                f"{self.profile.representation} representation over the "
                f"{self.profile.kernels} kernels, split "
                f"{[list(item[1:]) for item in self.profile.layer_split]}, a TLS "
                "https origin on loopback, and the promoted seeded fault proxy on "
                "every leg"
            ),
        )
        self.driver_recorder = TraceRecorder(
            DRIVER_HOST, run=self.run, role="emulation_driver:route+gateway"
        )
        return self.run

    def load_tokenizer(self) -> Any:
        from tokenizers import Tokenizer

        tokenizer_path = self.model_root / "tokenizer.json"
        if not tokenizer_path.is_file():
            raise EmulationError(f"tokenizer_missing:{tokenizer_path}")
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        return self.tokenizer

    def chatml_prompt_ids(self) -> list[int]:
        """The real tokenizer over the real Qwen2.5 ChatML template."""

        text = (
            f"<|im_start|>system\n{CHATML_SYSTEM}<|im_end|>\n"
            f"<|im_start|>user\n{PROMPT}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        return list(self.tokenizer.encode(text).ids)

    def spawn_workers(self) -> None:
        """Launch the two worker processes, each with its own home and identity."""

        emulation_path = str(Path(__file__).resolve())
        environment = clean_subprocess_environment()
        for node_id, start_layer, end_layer in self.profile.layer_split:
            home = self.homes_dir / node_id
            log_path = self.logs_dir / f"{node_id}.log"
            placement_id = f"{node_id}-placement"
            arguments = [
                self.python,
                "-B",
                emulation_path,
                "worker",
                "--node-id",
                node_id,
                "--placement-id",
                placement_id,
                "--home",
                str(home),
                "--model-root",
                str(self.model_root),
                "--start-layer",
                str(start_layer),
                "--end-layer",
                str(end_layer),
                "--cleanup-latency-ms",
                "60",
                "--run-dir",
                str(self.run_dir),
                "--model-id",
                self.profile.model_id,
                "--model-revision",
                self.profile.revision,
                "--representation",
                self.profile.representation,
                "--kernels",
                self.profile.kernels,
                "--expected-layers",
                str(self.profile.expected_layers),
            ]
            if start_layer == 0:
                arguments.append("--owns-embed")
            if self.profile.owns_head(end_layer):
                arguments.append("--owns-head")
            if self.profile.sharded:
                arguments.append("--sharded")
            # worker-c runs on a deliberately wrong clock: the trace must correct it
            # rather than silently align it (mycelium_testbed/DESIGN_NOTES.md §3).
            if node_id == "worker-c":
                arguments += [
                    "--monotonic-offset-ns",
                    str(1_250_000_000),
                    "--wall-offset-ns",
                    str(-2_500_000_000),
                ]
            log_handle = open(log_path, "w", encoding="utf-8")
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=log_handle,
                text=True,
                env=environment,
                cwd=str(_REPO_ROOT),
            )
            runtime = WorkerRuntime(
                node_id=node_id,
                placement_id=placement_id,
                home=home,
                process=process,
                log_path=log_path,
            )
            runtime_counters = self._await_listening(runtime)
            runtime.port = int(runtime_counters["port"])
            runtime.identity = runtime_counters["identity"]
            runtime.stage = runtime_counters["stage"]
            self.workers[node_id] = runtime

    def _await_listening(
        self, runtime: WorkerRuntime, timeout: float = 180.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        assert runtime.process.stdout is not None
        while time.monotonic() < deadline:
            line = runtime.process.stdout.readline()
            if not line:
                if runtime.process.poll() is not None:
                    raise EmulationError(
                        f"worker_exited_before_listening:{runtime.node_id}:"
                        f"rc={runtime.process.returncode}:see {runtime.log_path}"
                    )
                continue
            try:
                document = json.loads(line)
            except json.JSONDecodeError:
                continue
            if document.get("event") == "listening":
                return document
        raise EmulationError(f"worker_listen_timeout:{runtime.node_id}")

    async def start_relays(self) -> None:
        """One relay per leg, shaped to the round-50 numbers."""

        assert self.origin is not None and self.origin.port is not None
        origin_leg = self._leg("origin")
        self.origin_relay = await start_proxy(
            origin_leg.config,
            "127.0.0.1",
            0,
            "127.0.0.1",
            self.origin.port,
            name="origin",
            leg="origin",
        )
        for node_id in WORKER_HOSTS:
            runtime = self.workers[node_id]
            leg = self._leg(node_id)
            runtime.relay = await start_proxy(
                leg.config,
                "127.0.0.1",
                0,
                "127.0.0.1",
                runtime.port,
                name=node_id,
                leg="worker",
            )

    # -- memory envelope ---------------------------------------------------- #

    async def sample_worker_memory(self, *, interval_s: float = 0.1) -> None:
        """Sample each worker's RSS with ``ps`` while the requests run.

        The peak is what a reader needs for the single-host memory envelope, and it is
        observed from outside the worker rather than reported by the worker about
        itself (``RSS_SAMPLING_METHOD``).  It stays an *observation*: it is recorded
        with its method and never asserted, because under memory pressure the OS can
        reclaim and compress pages and report a peak below what the stage loaded.
        """

        self._sampling = True
        while self._sampling:
            for runtime in self.workers.values():
                rss = await asyncio.to_thread(_read_rss_bytes, runtime.process.pid)
                if rss is not None:
                    runtime.rss_samples += 1
                    runtime.peak_rss_bytes = max(runtime.peak_rss_bytes, rss)
            await asyncio.sleep(interval_s)

    def stop_memory_sampling(self) -> None:
        self._sampling = False

    # -- one worker round trip --------------------------------------------- #

    async def call(
        self,
        node_id: str,
        header: Mapping[str, Any],
        payload: bytes = b"",
        *,
        timeout: float = 30.0,
    ) -> tuple[dict[str, Any], bytes, float]:
        """One command over the shaped relay path, with the RTT measured."""

        runtime = self.workers[node_id]
        assert runtime.relay is not None
        started = time.perf_counter()
        reader, writer = await asyncio.open_connection("127.0.0.1", runtime.relay.port)
        try:
            writer.write(encode_frame(header, payload))
            await writer.drain()
            reply_header, reply_payload = await asyncio.wait_for(
                read_frame(reader), timeout=timeout
            )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if reply_header.get("ok") is not True:
            raise EmulationError(
                f"worker_command_failed:{node_id}:{header.get('op')}:"
                f"{reply_header.get('error')}"
            )
        return reply_header, reply_payload, elapsed_ms

    async def clock_exchange(self, node_id: str) -> ExchangeSample:
        """A real four-timestamp exchange through the relay, for the trace."""

        assert self.driver_recorder is not None
        send = HostClock().stamp()
        reply, _payload, _rtt = await self.call(
            node_id,
            {
                "op": "ping",
                "request_id": f"clock-exchange-{node_id}",
                "command_id": f"cmd-ping-{node_id}",
            },
        )
        receive = HostClock().stamp()
        return ExchangeSample(
            a_host=DRIVER_HOST,
            b_host=node_id,
            a_send_monotonic_ns=send.monotonic_ns,
            a_send_wall_ns=send.wall_ns,
            b_recv_monotonic_ns=int(reply["worker_recv_monotonic_ns"]),
            b_recv_wall_ns=int(reply["worker_recv_wall_ns"]),
            b_send_monotonic_ns=int(reply["worker_send_monotonic_ns"]),
            b_send_wall_ns=int(reply["worker_send_wall_ns"]),
            a_recv_monotonic_ns=receive.monotonic_ns,
            a_recv_wall_ns=receive.wall_ns,
            source="emulation_clock_exchange",
            correlation={"node_id": node_id, "relay": f"worker:{node_id}"},
        )

    # -- the request -------------------------------------------------------- #

    async def run_successful_request(
        self, events: "queue.Queue[str | None]", request_id: str
    ) -> dict[str, Any]:
        """The C4->C5 chain: fanout, streamed tokens, receipts, terminal frame."""

        recorder = self.driver_recorder
        run = self.run
        if recorder is None or run is None:
            raise EmulationError("driver_run_not_open")
        if self.origin is None or self.origin_relay is None:
            raise EmulationError("origin_not_started")
        origin_port = self.origin.port
        origin_relay_port = self.origin_relay.port
        prompt_ids = self.chatml_prompt_ids()
        recorder.event(
            EventKind.ENQUEUE,
            request_id=request_id,
            detail={
                "origin": "gateway_submit",
                "prompt": PROMPT,
                "prompt_tokens": len(prompt_ids),
                "placements": [
                    self.workers[node].placement_id for node in WORKER_HOSTS
                ],
                "model": {
                    "id": self.profile.model_id,
                    "revision": self.profile.revision,
                    "representation": self.profile.representation,
                    "kernels": self.profile.kernels,
                },
            },
        )
        prompt_text = self.tokenizer.decode(prompt_ids)
        events.put(
            sse_event(
                "accepted",
                {
                    "request_id": request_id,
                    "prompt_tokens": len(prompt_ids),
                    "placements": [
                        self.workers[node].placement_id for node in WORKER_HOSTS
                    ],
                },
            )
        )

        first, second = WORKER_HOSTS
        tokens: list[int] = []
        receipts: dict[str, dict[str, Any]] = {}
        work: dict[str, dict[str, Any]] = {
            node: {
                "node_id": node,
                "placement_id": self.workers[node].placement_id,
                "layers": self.workers[node].stage["layers"],
                "commands": 0,
                "layers_executed": 0,
                "tokens_processed": 0,
                "hidden_bytes_in": 0,
                "hidden_bytes_out": 0,
                "model_ms_total": 0.0,
            }
            for node in WORKER_HOSTS
        }

        async def send(
            node_id: str,
            *,
            phase: str,
            token_index: int,
            extra: Mapping[str, Any],
            payload: bytes = b"",
        ) -> tuple[dict[str, Any], bytes, float]:
            command_id = f"cmd-{request_id}-{node_id}-{phase}-{token_index}"
            header = {
                "op": "layers",
                "request_id": request_id,
                "command_id": command_id,
                "placement_id": self.workers[node_id].placement_id,
                "phase": phase,
                "token_index": token_index,
                **extra,
            }
            digest = frame_digest(header, payload)
            recorder.event(
                EventKind.ENQUEUE,
                request_id=request_id,
                command_id=command_id,
                placement_id=self.workers[node_id].placement_id,
                detail={
                    "phase": phase,
                    "token_index": token_index,
                    "frame_digest": digest,
                    "bytes": len(payload),
                    "origin": "route.fanout",
                },
            )
            reply, reply_payload, rtt_ms = await self.call(node_id, header, payload)
            reply_digest = frame_digest(reply, reply_payload)
            recorder.event(
                EventKind.RECEIPT,
                request_id=request_id,
                command_id=command_id,
                placement_id=self.workers[node_id].placement_id,
                detail={
                    "phase": phase,
                    "token_index": token_index,
                    "frame_digest": reply_digest,
                    "command_digest": digest,
                    "rtt_ms": round(rtt_ms, 4),
                    "semantics": "peer_ack",
                    "origin": "route.fanout",
                },
            )
            entry = work[node_id]
            entry["commands"] += 1
            entry["layers_executed"] += int(reply.get("layers_executed", 0))
            entry["tokens_processed"] += int(reply.get("token_id") is not None)
            entry["model_ms_total"] += float(reply.get("model_ms", 0.0))
            entry["hidden_bytes_in"] += len(payload)
            entry["hidden_bytes_out"] += len(reply_payload)
            entry["kv_len"] = int(reply.get("kv_len", 0))
            return reply, reply_payload, rtt_ms

        # -- prefill, then one decode step per streamed token -- #
        hidden_reply, hidden_payload, _ = await send(
            first,
            phase="prefill",
            token_index=0,
            extra={"token_ids": prompt_ids, "sequence": len(prompt_ids), "position": 0},
        )
        token_reply, _token_payload, _ = await send(
            second,
            phase="prefill",
            token_index=0,
            extra={
                "sequence": len(prompt_ids),
                "hidden": hidden_reply["hidden"],
                "position": 0,
            },
            payload=hidden_payload,
        )
        next_token = int(token_reply["token_id"])

        for step in range(self.token_steps):
            tokens.append(next_token)
            delta = self.tokenizer.decode([next_token])
            events.put(
                sse_event(
                    "token",
                    {
                        "request_id": request_id,
                        "token_index": step,
                        "token_id": next_token,
                        "text": delta,
                    },
                )
            )
            hidden_reply, hidden_payload, _ = await send(
                first,
                phase="decode",
                token_index=step + 1,
                extra={
                    "token_id": next_token,
                    "position": len(prompt_ids) + step,
                    **self.entry_sequence_argument(prompt_ids, tokens),
                },
            )
            token_reply, _token_payload, _ = await send(
                second,
                phase="decode",
                token_index=step + 1,
                extra={
                    "hidden": hidden_reply["hidden"],
                    "position": len(prompt_ids) + step,
                    # the sequence the entry placement just ran forward, so a
                    # cacheless continuation placement shapes its hidden payload
                    "sequence": len(prompt_ids) + step + 1,
                },
                payload=hidden_payload,
            )
            next_token = int(token_reply["token_id"])

        payload_counters = {}
        for node_id in WORKER_HOSTS:
            reply, _payload, _ = await self.call(
                node_id,
                {
                    "op": "counters",
                    "request_id": request_id,
                    "command_id": f"cmd-{request_id}-{node_id}-counters",
                },
            )
            payload_counters[node_id] = reply["counters"]

        # request-scoped cleanup through the same shaped path (the round-50 path)
        cleanup = await self.request_cleanup(request_id)
        receipts = cleanup.receipts
        terminal_status = "completed" if cleanup.status == "ok" else cleanup.status
        generated = self.tokenizer.decode(tokens)
        recorder.event(
            EventKind.TERMINAL,
            request_id=request_id,
            status=terminal_status,
            detail={
                "origin": "gateway_terminal",
                "token_indexes": list(range(len(tokens))),
                "tokens": tokens,
                "generated_text": generated,
                "prompt_text_prefix": prompt_text[:64],
                "receipts": receipts,
                "placement_work": list(work.values()),
                "cleanup": cleanup.to_document(),
                "worker_counters": payload_counters,
                "tls_origin": {
                    "url": f"https://127.0.0.1:{origin_port}/stream",
                    "relay_port": origin_relay_port,
                },
            },
        )
        events.put(
            sse_event(
                "terminal",
                {
                    "request_id": request_id,
                    "status": terminal_status,
                    "token_indexes": list(range(len(tokens))),
                    "tokens": tokens,
                    "generated_text": generated,
                    "receipts": [
                        {
                            "placement_id": value["placement_id"],
                            "node_id": value["node_id"],
                            "receipt_digest": value["receipt_digest"],
                            "cleanup_ms": value["cleanup_ms"],
                        }
                        for value in receipts.values()
                    ],
                    "placement_work": list(work.values()),
                    "acknowledged_cursor": len(tokens),
                    # Deliberately nothing readiness-shaped.  A readiness claim is
                    # the qualification authority's to mint, not a harness's: the
                    # frame carries what this run can account for (the cursor, the
                    # receipts, the placement work) and no claim at all.  See
                    # tests/testbed/test_harness_claim_boundary.py.
                },
            )
        )
        return {
            "request_id": request_id,
            "phase": "success",
            "terminal_status": terminal_status,
            "tokens": tokens,
            "generated_text": generated,
            "receipts": receipts,
            "placement_work": list(work.values()),
            "cleanup": cleanup.to_document(),
            "worker_counters": payload_counters,
        }

    # -- cleanup, with the route-side poll measured ------------------------- #

    async def request_cleanup(
        self, request_id: str, *, use_hold: bool = False
    ) -> CleanupOutcome:
        """Fan out cleanup and wait for every receipt inside the fanout window.

        The wait is the route's: a bounded cadence poll of a receipt registry at
        ``mycelium_deadlines.CLEANUP_RECEIPT_POLL_SECONDS`` against the absolute
        fanout-window deadline, raising the named failure when it expires.
        """

        recorder = self.driver_recorder
        assert recorder is not None
        window_ms = float(route_cleanup_fanout_window_ms)
        budget_ms = CANCEL_AND_CLEANUP_BUDGET_SECONDS * 1000.0
        started = time.monotonic()
        deadline = started + window_ms / 1000.0
        recorder.event(
            EventKind.ENQUEUE,
            request_id=request_id,
            detail={
                "origin": "route.cleanup_fanout",
                "window_ms": window_ms,
                "budget_ms": budget_ms,
                "poll_seconds": CLEANUP_RECEIPT_POLL_SECONDS,
                "peer_advertised_cleanup_ms": advertised_maximum_total_cleanup_ms,
            },
        )
        registry: dict[str, dict[str, Any]] = {}
        sealed: dict[str, float] = {}
        self._receipts[request_id] = registry
        self._sealed_at[request_id] = sealed
        armed: list[str] = []
        if use_hold and self.receipt_hold_ms:
            runtime = self.workers["worker-c"]
            if runtime.relay is None:
                raise EmulationError("worker_c_relay_missing")
            runtime.relay.set_hold(self.receipt_hold_ms)
            armed.append("worker-c")
        tasks: dict[str, "asyncio.Task[Any]"] = {}
        poll_waits: list[float] = []
        status = "ok"
        blockers: list[str] = []
        try:
            for node_id in WORKER_HOSTS:
                tasks[node_id] = asyncio.create_task(
                    self.call(
                        node_id,
                        {
                            "op": "cleanup",
                            "request_id": request_id,
                            "command_id": f"cmd-{request_id}-{node_id}-cleanup",
                        },
                        timeout=(max(5.0, self.receipt_hold_ms or 0.0) / 1000.0) + 5.0,
                    )
                )
            for node_id, task in tasks.items():
                task.add_done_callback(
                    lambda completed, node=node_id: self._seal_receipt(
                        request_id, node, completed
                    )
                )
            # The route's own wait: a bounded cadence poll of the receipt registry
            # against the absolute fanout-window deadline.  ``await`` rather than
            # ``sleep`` so the receipts can actually land - a blocking sleep here is
            # exactly the thread/asyncio bridge stall the research note warns about
            # (research-mailbox-longpoll.md 4.8).
            while True:
                if len(registry) >= len(WORKER_HOSTS):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = sorted(set(WORKER_HOSTS) - set(registry))
                    status = (
                        f"{CLEANUP_RECEIPT_TIMEOUT}|"
                        f"missing_cleanup_receipts={','.join(missing)}"
                    )
                    blockers = missing
                    break
                before = time.monotonic()
                await asyncio.sleep(min(CLEANUP_RECEIPT_POLL_SECONDS, remaining))
                poll_waits.append((time.monotonic() - before) * 1000.0)
        finally:
            for node_id in armed:
                runtime = self.workers[node_id]
                if runtime.relay is not None:
                    runtime.relay.set_hold(0.0)
        noticed = time.monotonic()
        if status == "ok" and sealed:
            observation_delay_ms = (noticed - max(sealed.values())) * 1000.0
        else:
            observation_delay_ms = max(0.0, (noticed - deadline) * 1000.0)
        waited_ms = (noticed - started) * 1000.0
        outcome = CleanupOutcome(
            request_id=request_id,
            receipts=registry,
            missing=tuple(blockers),
            status=status,
            observation_delay_ms=observation_delay_ms,
            poll_waits_ms=poll_waits,
            waited_ms=waited_ms,
            window_ms=window_ms,
            budget_ms=budget_ms,
        )
        document = outcome.to_document()
        recorder.event(
            EventKind.TERMINAL if status != "ok" else EventKind.RECEIPT,
            request_id=request_id,
            status=status,
            detail={
                "origin": "route.cleanup_observation",
                "receipts": sorted(registry),
                "missing": list(blockers),
                "waited_ms": round(waited_ms, 4),
                "deadline_ms": window_ms,
                "observation_delay_ms": document["observation_delay_ms"],
                "poll_waits": document["poll_waits"],
            },
        )
        for task in tasks.values():
            task.cancel()
        return outcome

    def _seal_receipt(
        self, request_id: str, node_id: str, task: "asyncio.Task[Any]"
    ) -> None:
        """Store a receipt under the monotonic registry the route polls."""

        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._receipts.setdefault(request_id, {})[f"{node_id}:error"] = {
                "node_id": node_id,
                "error": f"{type(error).__name__}:{error}"[:200],
            }
            return
        reply_header, _payload, rtt_ms = task.result()
        receipt = dict(reply_header["receipt"])
        receipt["rtt_ms"] = round(rtt_ms, 4)
        registry = self._receipts.setdefault(request_id, {})
        registry[node_id] = receipt
        self._sealed_at.setdefault(request_id, {})[node_id] = time.monotonic()
        recorder = self.driver_recorder
        if recorder is not None:
            recorder.event(
                EventKind.RECEIPT,
                request_id=request_id,
                command_id=receipt["command_id"],
                placement_id=receipt["placement_id"],
                status="ok",
                detail={
                    "origin": "route.cleanup_receipt",
                    "receipt_kind": "cleanup",
                    "receipt_digest": receipt["receipt_digest"],
                    "cleanup_ms": receipt["cleanup_ms"],
                    "rtt_ms": receipt["rtt_ms"],
                },
            )

    # -- the route-side receipt grid, measured on this host ---------------- #

    async def sample_receipt_observation_grid(self, trials: int = 40) -> dict[str, Any]:
        """Measure the added latency of the route's receipt cadence on this host.

        A receipt becomes ready at a uniformly random phase inside one cadence
        tick; the poller sleeps the cadence and then looks.  The observation delay
        is therefore bounded by ``CLEANUP_RECEIPT_POLL_SECONDS`` and, per the
        research note, is the round-50 witness path: the true 100 ms on the
        cleanup-receipt path is this grid, not the SSE bridge.
        """

        rng = random.Random(f"{self.seed}:receipt-grid")
        observations: list[float] = []
        for _ in range(max(1, trials)):
            phase = rng.uniform(0.0, CLEANUP_RECEIPT_POLL_SECONDS)
            receipt_ready = time.monotonic() + phase
            noticed: float | None = None
            while noticed is None:
                await asyncio.sleep(CLEANUP_RECEIPT_POLL_SECONDS)
                if time.monotonic() >= receipt_ready:
                    noticed = time.monotonic()
            observations.append((noticed - receipt_ready) * 1000.0)
        mean = sum(observations) / len(observations)
        return {
            "cadence_seconds": CLEANUP_RECEIPT_POLL_SECONDS,
            "source": "route.py:2770 time.sleep(min(_CLEANUP_RECEIPT_POLL_SECONDS, remaining))",
            "trials": len(observations),
            "min_ms": round(min(observations), 4),
            "mean_ms": round(mean, 4),
            "max_ms": round(max(observations), 4),
            "research_p3": {
                "mean_ms": ROUND50_RECEIPT_POLL_MEAN_MS,
                "p95_ms": ROUND50_RECEIPT_POLL_P95_MS,
                "max_ms": ROUND50_RECEIPT_POLL_MAX_MS,
                "n": 200,
                "source": "research-mailbox-longpoll.md 4.3 phase P3",
            },
            "note": (
                f"n={len(observations)} cannot claim a p95 (the p99 estimator needs "
                "n=381 for +/-1pp at 95% confidence; research-mailbox-longpoll.md "
                "4.1), so min/mean/max are reported and compared with the P3 sample"
            ),
        }

    # -- the negative case -------------------------------------------------- #

    async def run_negative_case(
        self, events: "queue.Queue[str | None]", request_id: str
    ) -> dict[str, Any]:
        """Stream a few tokens, then let the receipt hold break the request.

        Reproduces the round-50 shape: the peer's cleanup receipt exists but is
        observed after the route's fanout window, so the request must end in the
        named terminal failure inside its budget - and it must *end*.
        """

        recorder = self.driver_recorder
        assert recorder is not None
        prompt_ids = self.chatml_prompt_ids()
        recorder.event(
            EventKind.ENQUEUE,
            request_id=request_id,
            detail={
                "origin": "gateway_submit",
                "prompt": PROMPT,
                "prompt_tokens": len(prompt_ids),
                "negative_case": True,
                "receipt_hold_ms": self.receipt_hold_ms,
                "window_ms": route_cleanup_fanout_window_ms,
            },
        )
        events.put(
            sse_event("accepted", {"request_id": request_id, "negative_case": True})
        )
        first, second = WORKER_HOSTS
        tokens: list[int] = []
        started = time.monotonic()
        hidden_reply, hidden_payload, _ = await self.call(
            first,
            {
                "op": "layers",
                "request_id": request_id,
                "command_id": f"cmd-{request_id}-{first}-prefill-0",
                "placement_id": self.workers[first].placement_id,
                "phase": "prefill",
                "token_index": 0,
                "token_ids": prompt_ids,
                "sequence": len(prompt_ids),
                "position": 0,
            },
        )
        token_reply, _payload, _ = await self.call(
            second,
            {
                "op": "layers",
                "request_id": request_id,
                "command_id": f"cmd-{request_id}-{second}-prefill-0",
                "placement_id": self.workers[second].placement_id,
                "phase": "prefill",
                "token_index": 0,
                "sequence": len(prompt_ids),
                "hidden": hidden_reply["hidden"],
                "position": 0,
            },
            hidden_payload,
        )
        next_token = int(token_reply["token_id"])
        for step in range(NEGATIVE_TOKEN_STEPS):
            tokens.append(next_token)
            events.put(
                sse_event(
                    "token",
                    {
                        "request_id": request_id,
                        "token_index": step,
                        "token_id": next_token,
                        "text": self.tokenizer.decode([next_token]),
                    },
                )
            )
            hidden_reply, hidden_payload, _ = await self.call(
                first,
                {
                    "op": "layers",
                    "request_id": request_id,
                    "command_id": f"cmd-{request_id}-{first}-decode-{step + 1}",
                    "placement_id": self.workers[first].placement_id,
                    "phase": "decode",
                    "token_index": step + 1,
                    "token_id": next_token,
                    "position": len(prompt_ids) + step,
                    **self.entry_sequence_argument(prompt_ids, tokens),
                },
            )
            token_reply, _payload, _ = await self.call(
                second,
                {
                    "op": "layers",
                    "request_id": request_id,
                    "command_id": f"cmd-{request_id}-{second}-decode-{step + 1}",
                    "placement_id": self.workers[second].placement_id,
                    "phase": "decode",
                    "token_index": step + 1,
                    "hidden": hidden_reply["hidden"],
                    "position": len(prompt_ids) + step,
                    "sequence": len(prompt_ids) + step + 1,
                },
                hidden_payload,
            )
            next_token = int(token_reply["token_id"])

        # The cancellation arrives; the route fans out cleanup against its window.
        cancellation_started = time.monotonic()
        cleanup = await self.request_cleanup(request_id, use_hold=True)
        cancelled_to_terminal_ms = (time.monotonic() - cancellation_started) * 1000.0
        elapsed_ms = (time.monotonic() - started) * 1000.0
        terminal_status = cleanup.status
        recorder.event(
            EventKind.TERMINAL,
            request_id=request_id,
            status=terminal_status,
            detail={
                "origin": "gateway_terminal",
                "negative_case": True,
                "tokens_before_failure": tokens,
                "cleanup": cleanup.to_document(),
                "elapsed_ms": round(elapsed_ms, 4),
                "cancellation_to_terminal_ms": round(cancelled_to_terminal_ms, 4),
                "fanout_window_ms": cleanup.window_ms,
                "budget_ms": cleanup.budget_ms,
                "receipt_hold_ms": self.receipt_hold_ms,
                "receipts_missing": list(cleanup.missing),
                "never_stuck": True,
            },
        )
        events.put(
            sse_event(
                "terminal",
                {
                    "request_id": request_id,
                    "status": terminal_status,
                    "tokens_before_failure": tokens,
                    "receipts_missing": list(cleanup.missing),
                    "elapsed_ms": round(elapsed_ms, 4),
                    "cancellation_to_terminal_ms": round(cancelled_to_terminal_ms, 4),
                    "fanout_window_ms": cleanup.window_ms,
                    "budget_ms": cleanup.budget_ms,
                },
            )
        )
        return {
            "request_id": request_id,
            "phase": "negative",
            "terminal_status": terminal_status,
            "tokens": tokens,
            "cleanup": cleanup.to_document(),
            "elapsed_ms": elapsed_ms,
            "cancellation_to_terminal_ms": cancelled_to_terminal_ms,
        }

    # -- teardown ----------------------------------------------------------- #

    def shutdown(self) -> None:
        for runtime in self.workers.values():
            if runtime.relay is not None:
                runtime.relay.stop()
            if runtime.process.poll() is None:
                runtime.process.terminate()
                try:
                    runtime.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    runtime.process.kill()
            if runtime.process.stdout is not None:
                runtime.process.stdout.close()
        if self.origin is not None:
            self.origin.stop()
        if self.run is not None:
            self.run.close()


def _product_digest(repository_root: Path, roots: Sequence[str]) -> str | None:
    digest = hashlib.sha256()
    files: list[Path] = []
    for name in roots:
        root = repository_root / name
        if not root.is_dir():
            continue
        files.extend(
            path
            for path in root.rglob("*.py")
            if path.is_file() and "__pycache__" not in path.parts
        )
    if not files:
        return None
    for path in sorted(files):
        digest.update(path.relative_to(repository_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #


async def _run_async(
    *,
    run_dir: Path,
    model_root: Path,
    seed: int,
    receipt_hold_ms: float | None,
    python: str | None,
    fsync: bool,
    token_steps: int,
    profile: ModelProfile | None = None,
) -> EmulationResult:
    resolved_profile = profile or pilot_profile()
    started_wall = time.monotonic()
    emulation = Emulation(
        run_dir=run_dir,
        model_root=model_root,
        seed=seed,
        receipt_hold_ms=receipt_hold_ms,
        python=python,
        fsync=fsync,
        token_steps=token_steps,
        profile=resolved_profile,
    )
    run = emulation.open_run()
    emulation.load_tokenizer()
    emulation.origin = SseOrigin(emulation.tls_dir)
    emulation.origin.start()
    emulation.spawn_workers()
    await emulation.start_relays()

    ok_request = "wp2-4-emulation-ok-001"
    negative_request = "wp2-4-emulation-receipt-held-002"
    requests: dict[str, Any] = {}
    streams: dict[str, Any] = {}
    samples: list[ExchangeSample] = []
    origin = emulation.origin
    origin_relay = emulation.origin_relay
    if origin is None or origin_relay is None:
        raise EmulationError("origin_not_started")
    if emulation.receipt_hold_ms is None:
        raise EmulationError("negative_case_requires_a_receipt_hold")
    memory_sampler: "asyncio.Task[None] | None" = None
    try:
        memory_sampler = asyncio.create_task(emulation.sample_worker_memory())
        for node_id in WORKER_HOSTS:
            reply, _payload, _rtt = await emulation.call(
                node_id,
                {
                    "op": "hello",
                    "request_id": "handshake",
                    "command_id": f"cmd-handshake-{node_id}",
                },
            )
            emulation.workers[node_id].identity = reply["identity"]
            emulation.workers[node_id].stage = reply["stage"]
            sample = await emulation.clock_exchange(node_id)
            samples.append(sample)
            run.record_exchange(sample)
        # Scenario 1 (successful terminal) runs first, on legs that carry no hold;
        # scenario 2 arms the receipt hold for its cleanup exchange only.
        for handler, request_id in (
            (emulation.run_successful_request, ok_request),
            (emulation.run_negative_case, negative_request),
        ):
            with origin.exchange() as events:
                client = asyncio.create_task(
                    asyncio.to_thread(
                        _read_stream_over_tls,
                        "127.0.0.1",
                        origin_relay.port,
                        origin.client_cert_path(),
                        path=f"/stream?request_id={request_id}",
                        timeout=120.0,
                    )
                )
                await asyncio.to_thread(origin.wait_for_client, 20.0)
                requests[request_id] = await handler(events, request_id)
                events.put(None)
                streams[request_id] = await client
        grid_measurement = await emulation.sample_receipt_observation_grid()
    finally:
        emulation.stop_memory_sampling()
        if memory_sampler is not None:
            memory_sampler.cancel()
        emulation.shutdown()

    loaded = load_run(run_dir)
    summary = summarise(run_dir, reference_host=DRIVER_HOST)
    (run_dir / "summary.txt").write_text(summary, encoding="utf-8")
    try:
        offsets = estimate_offsets_by_host(loaded.samples, reference_host=DRIVER_HOST)
    except TraceError:
        offsets = {}
    timeline = merge_timeline(loaded, offsets, reference_host=DRIVER_HOST)

    d9_reason = check_peer_advertised_cleanup_bound(
        advertised_cleanup_ms=advertised_maximum_total_cleanup_ms,
        fanout_window_ms=route_cleanup_fanout_window_ms,
    )
    negative = requests[negative_request]
    # What the checkpoint itself says the placements must hold, read from the
    # safetensors header: deterministic, and independent of both the stage's
    # accounting and the sampled RSS.
    snapshot_facts, snapshot_tensor_bytes = snapshot_memory_facts(
        model_root,
        representation=resolved_profile.representation,
        sharded=resolved_profile.sharded,
    )
    worker_documents: dict[str, dict[str, Any]] = {}
    for node_id in WORKER_HOSTS:
        entry = emulation.workers[node_id].to_document()
        stage = dict(entry.get("stage") or {})
        layers = [int(value) for value in (stage.get("layers") or (0, 0))]
        expected_tensors, expected_bytes = placement_expected_bytes(
            snapshot_tensor_bytes,
            start_layer=layers[0],
            end_layer=layers[1],
            owns_embed=bool(stage.get("owns_embed")),
            owns_head=bool(stage.get("owns_head")),
            head_tied_to_embeddings=bool(stage.get("head_tied_to_embeddings")),
            head_tensor=str(stage.get("head_tensor") or "") or None,
        )
        entry["placement_expectation"] = {
            "resident_bytes": expected_bytes,
            "source": snapshot_facts["source"],
            "tensors": expected_tensors,
        }
        # the verdict a reader (or the readiness gate) can check without re-deriving
        # anything: the accounting is deterministic, the RSS is an observation
        entry["memory"] = evaluate_worker_memory_envelope(entry).to_document()
        worker_documents[node_id] = entry
    document: dict[str, Any] = {
        "protocol": "mycelium.testbed.emulation.v1",
        "seed": seed,
        "profile": resolved_profile.to_document(),
        "model": {
            "id": resolved_profile.model_id,
            "revision": resolved_profile.revision,
            "root": str(model_root),
            "source": "local HuggingFace cache; no download performed",
            "representation": resolved_profile.representation,
            "kernels": resolved_profile.kernels,
            "runtime_dtype": resolved_profile.runtime_dtype,
            "layer_split": [list(item) for item in resolved_profile.layer_split],
            "cacheless_placements": resolved_profile.cacheless,
            "safetensors": snapshot_facts,
        },
        "origin": {
            "url": f"https://127.0.0.1:{emulation.origin.port}/stream",
            "relay_port": emulation.origin_relay.port,
            "tls": "local CA + leaf, 127.0.0.1 SAN; no public hostname, no quick tunnel",
            "tls_dir": str(emulation.tls_dir),
            "reused": list(_reuse_notes()),
        },
        "workers": worker_documents,
        "home_digests": {
            node_id: _directory_digest(emulation.workers[node_id].home)
            for node_id in WORKER_HOSTS
        },
        "relays": {
            "origin": emulation.origin_relay.document(),
            **{
                node_id: emulation.workers[node_id].relay.document()
                for node_id in WORKER_HOSTS
            },
        },
        "round50_shaping": {
            "legs": [leg.to_document() for leg in emulation.legs],
            "measured": [
                {"symbol": symbol, "value": value, "unit": unit, "source": source}
                for symbol, value, unit, source in ROUND50_MEASURED
            ],
            "unmeasured": list(ROUND50_UNMEASURED),
            "route_poll_seconds": CLEANUP_RECEIPT_POLL_SECONDS,
            "route_fanout_window_ms": route_cleanup_fanout_window_ms,
            "cancel_and_cleanup_budget_ms": CANCEL_AND_CLEANUP_BUDGET_SECONDS * 1000.0,
            "peer_advertised_cleanup_ms": advertised_maximum_total_cleanup_ms,
            "receipt_hold_armed_ms_at_negative_cleanup": emulation.receipt_hold_ms,
            "d9_verdict": d9_reason
            or f"ok:{advertised_maximum_total_cleanup_ms}<= {route_cleanup_fanout_window_ms}",
        },
        "route_receipt_grid": grid_measurement,
        "requests": requests,
        "streams": {
            request_id: measurements.to_document()
            for request_id, measurements in streams.items()
        },
        "clock": {
            "reference_host": DRIVER_HOST,
            "samples": [sample.to_document() for sample in samples],
            "offsets": {host: offset.to_document() for host, offset in offsets.items()},
            "note": (
                "worker-c runs a deliberately skewed clock "
                "(monotonic +1.250s, wall -2.500s); only the corrected timeline is "
                "interleaved"
            ),
        },
        "timeline_events": len(timeline),
        "trace": {
            "run_dir": str(run_dir),
            "requests": sorted(requests),
            "events": len(loaded.events),
            "request_files": sorted(
                path.name for path in (run_dir / "requests").glob("*.jsonl")
            ),
            "command_files": len(list((run_dir / "commands").glob("*.jsonl"))),
            "exchanges": len(loaded.samples),
        },
        "status": {
            "ok_terminal": requests[ok_request]["terminal_status"],
            "negative_terminal": negative["terminal_status"],
            "negative_elapsed_ms": round(negative["elapsed_ms"], 4),
            "negative_cancellation_to_terminal_ms": round(
                negative["cancellation_to_terminal_ms"], 4
            ),
            "negative_fanout_window_ms": negative["cleanup"]["window_ms"],
            "negative_budget_ms": negative["cleanup"]["budget_ms"],
            "negative_within_budget": (
                negative["cancellation_to_terminal_ms"]
                <= negative["cleanup"]["budget_ms"]
            ),
            "negative_receipts_missing": negative["cleanup"]["missing"],
        },
    }
    document["measured"] = {
        "split": [list(item) for item in resolved_profile.layer_split],
        "wall_clock_s": round(time.monotonic() - started_wall, 4),
        "stage_accounted_bytes": {
            node_id: worker_documents[node_id]["memory"]["accounted"]["resident_bytes"]
            for node_id in WORKER_HOSTS
        },
        "stage_accounted_bytes_total": sum(
            worker_documents[node_id]["memory"]["accounted"]["resident_bytes"]
            for node_id in WORKER_HOSTS
        ),
        "worker_peak_rss_bytes": {
            node_id: worker_documents[node_id]["memory"]["observation"][
                "peak_rss_bytes"
            ]
            for node_id in WORKER_HOSTS
        },
        "worker_peak_rss_bytes_max": max(
            worker_documents[node_id]["memory"]["observation"]["peak_rss_bytes"]
            for node_id in WORKER_HOSTS
        ),
        "accounting_basis": STAGE_ACCOUNTING_BASIS,
        "rss_sampling_method": RSS_SAMPLING_METHOD,
        "worker_peak_rss_method": (
            "harness external sampler (ps -o rss= every 0.1s, started after both "
            "placements finished loading, so it covers the request phase only)"
        ),
        "worker_self_reported_peak_rss_bytes": {
            node_id: int(
                (requests.get(ok_request, {}).get("worker_counters", {}) or {}).get(
                    node_id, {}
                ).get("self_reported_peak_rss_bytes")
                or 0
            )
            for node_id in WORKER_HOSTS
        },
        "worker_self_reported_peak_rss_unit": "bytes",
        "worker_self_reported_peak_rss_method": (
            "the placement's own resource.getrusage(RUSAGE_SELF).ru_maxrss, a "
            "lifetime high-water mark that includes the load phase; reported "
            "beside the external sampler, never used as a verdict"
        ),
        "host_available_bytes": _host_available_bytes(),
        "cacheless_placements": resolved_profile.cacheless,
        "token_steps": token_steps,
    }
    (run_dir / "faults.json").write_text(
        json.dumps(
            {
                "legs": [leg.to_document() for leg in emulation.legs],
                "relays": {
                    "origin": emulation.origin_relay.document(),
                    **{
                        node_id: emulation.workers[node_id].relay.document()
                        for node_id in WORKER_HOSTS
                    },
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    stream_lines: list[str] = []
    for request_id in (ok_request, negative_request):
        measurements = streams.get(request_id)
        if measurements is None:
            continue
        stream_lines += [
            f"# request {request_id} via https://127.0.0.1:{origin_relay.port}"
            f"{'/stream?request_id='}{request_id}",
            f"status={measurements.status}",
            f"content-type={measurements.content_type}",
            f"transfer-encoding={measurements.transfer_encoding or 'absent (close-delimited body)'}",
            f"x-accel-buffering={measurements.x_accel_buffering}",
            f"tls_version={measurements.tls_version}",
            f"cipher={measurements.cipher}",
            f"peer_cert_subject={measurements.peer_cert_subject}",
            f"elapsed={measurements.elapsed_s:.3f}s "
            f"newlines={measurements.newline_chunks} "
            f"bytes={measurements.bytes_read} "
            f"max_inter_byte_gap={measurements.max_inter_byte_gap_s:.3f}s",
            "events:",
            *[
                f"  t={event['t_s']:.3f}s {event['event']}"
                for event in measurements.events
            ],
            "",
        ]
    if stream_lines:
        (run_dir / "client_stream.txt").write_text(
            "\n".join(stream_lines) + "\n", encoding="utf-8"
        )

    ok = requests.get(ok_request, {})
    negative_cleanup = negative["cleanup"]
    result = EmulationResult(
        run_dir=Path(run_dir),
        document=document,
        summary=summary,
        tokens=tuple(ok.get("tokens", ())),
        generated_text=ok.get("generated_text", ""),
        receipts=ok.get("receipts", {}),
        placement_work=tuple(ok.get("placement_work", ())),
        negative_terminal_status=negative["terminal_status"],
        negative_elapsed_ms=float(negative["elapsed_ms"]),
        negative_receipts_missing=tuple(negative_cleanup["missing"]),
        d9_reason=d9_reason,
        events=loaded.events,
    )
    # the run document carries the same summary keys the CLI prints, so a reader
    # does not have to dig into the per-request sections for the headline result
    (run_dir / "emulation.json").write_text(
        json.dumps(result.to_document(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _directory_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


# The negative case must hold the receipt PAST the route's real cleanup window or
# it is not a negative case.  1700 ms was calibrated against the
# pre-re-derivation 1500 ms window and went stale the moment the window moved
# (2026-09-20); derived here so it cannot go stale again.
NEGATIVE_CASE_RECEIPT_HOLD_MS: float = float(
    route_cleanup_fanout_window_ms + PEER_CLEANUP_BOUND_TRANSPORT_MARGIN_MS
)


def run_emulation(
    *,
    run_dir: Path | str,
    model_root: Path | str | None = None,
    seed: int = 20260915,
    receipt_hold_ms: float | None = NEGATIVE_CASE_RECEIPT_HOLD_MS,
    python: str | None = None,
    fsync: bool = True,
    token_steps: int = TOKEN_STEPS,
    profile: ModelProfile | None = None,
) -> EmulationResult:
    """Run the emulation once.  Raises :class:`EmulationError` on a stop condition."""

    resolved_profile = profile or pilot_profile()
    resolved_root = _require_model(
        Path(model_root) if model_root else resolved_profile.model_root(),
        sharded=resolved_profile.sharded,
    )
    return asyncio.run(
        _run_async(
            run_dir=Path(run_dir),
            model_root=resolved_root,
            seed=seed,
            receipt_hold_ms=receipt_hold_ms,
            python=python,
            fsync=fsync,
            token_steps=token_steps,
            profile=resolved_profile,
        )
    )


def emulation_document(result: EmulationResult) -> dict[str, Any]:
    return result.to_document()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="single-host two-worker C4->C5 emulation (PLAN 5.2 item 3)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("worker", help="run one emulation worker process")
    worker.add_argument("--node-id", required=True)
    worker.add_argument("--placement-id", required=True)
    worker.add_argument("--home", type=Path, required=True)
    worker.add_argument("--model-root", type=Path, required=True)
    worker.add_argument("--start-layer", type=int, required=True)
    worker.add_argument("--end-layer", type=int, required=True)
    worker.add_argument("--owns-embed", action="store_true")
    worker.add_argument("--owns-head", action="store_true")
    worker.add_argument("--cleanup-latency-ms", type=float, default=60.0)
    worker.add_argument("--run-dir", type=Path, default=None)
    worker.add_argument("--monotonic-offset-ns", type=int, default=0)
    worker.add_argument("--wall-offset-ns", type=int, default=0)
    worker.add_argument("--listen-host", default="127.0.0.1")
    worker.add_argument("--model-id", default=MODEL_ID)
    worker.add_argument("--model-revision", default=MODEL_REVISION)
    worker.add_argument("--representation", default=REPRESENTATION_SOURCE)
    worker.add_argument("--kernels", default=KERNELS_TESTBED)
    worker.add_argument("--expected-layers", type=int, default=None)
    worker.add_argument("--sharded", action="store_true")

    run = sub.add_parser("run", help="run the whole emulation")
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--model-root", type=Path, default=None)
    run.add_argument("--seed", type=int, default=20260915)
    run.add_argument(
        "--receipt-hold-ms", type=float, default=NEGATIVE_CASE_RECEIPT_HOLD_MS
    )
    run.add_argument(
        "--generation-steps", dest="token_steps", type=int, default=TOKEN_STEPS
    )
    run.add_argument("--profile", choices=sorted(MODEL_PROFILES), default="pilot")
    args = parser.parse_args(argv)

    if args.command == "worker":
        return worker_main(
            [
                "--node-id",
                args.node_id,
                "--placement-id",
                args.placement_id,
                "--home",
                str(args.home),
                "--model-root",
                str(args.model_root),
                "--start-layer",
                str(args.start_layer),
                "--end-layer",
                str(args.end_layer),
                *(["--owns-embed"] if args.owns_embed else []),
                *(["--owns-head"] if args.owns_head else []),
                "--cleanup-latency-ms",
                str(args.cleanup_latency_ms),
                *(["--run-dir", str(args.run_dir)] if args.run_dir else []),
                "--monotonic-offset-ns",
                str(args.monotonic_offset_ns),
                "--wall-offset-ns",
                str(args.wall_offset_ns),
                "--listen-host",
                args.listen_host,
                "--model-id",
                args.model_id,
                "--model-revision",
                args.model_revision,
                "--representation",
                args.representation,
                "--kernels",
                args.kernels,
                *(
                    ["--expected-layers", str(args.expected_layers)]
                    if args.expected_layers is not None
                    else []
                ),
                *(["--sharded"] if args.sharded else []),
            ]
        )
    result = run_emulation(
        run_dir=args.run_dir,
        model_root=args.model_root,
        seed=args.seed,
        receipt_hold_ms=args.receipt_hold_ms,
        token_steps=args.token_steps,
        profile=MODEL_PROFILES[args.profile](),
    )
    print(json.dumps(result.to_document(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
