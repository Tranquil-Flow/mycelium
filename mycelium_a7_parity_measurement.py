# SPDX-License-Identifier: AGPL-3.0-only
"""Drive the A7 cross-host standby parity challenge across three placement workers.

The entry (stage 0), the source (covered stage, ACTIVE) and the standby (same
stage, STANDBY) each run :mod:`mycelium_a7_parity_worker` on their own host;
how each worker is launched (locally, or over ssh into a staged candidate
checkout) is configuration only.  The driver executes, in order:

1. entry PREFILL of the challenge prompt -> stage-0 activations;
2. source PREFILL of those activations -> the first token;
3. source KV checkpoint export at the committed position (the exact bytes the
   standby would import at a cutover);
4. entry DECODE of the first token -> the next-position activations;
5. source DECODE and standby (import, then DECODE) of the *same*
   next-position activations on their own hosts;
6. both placements' exact KV identities.

Outputs are compared by digest only.  A match seals the closed
``mycelium.a7_standby_parity_challenge.v1`` document through
:func:`mycelium_a7_standby_qualification.physical_parity_challenge`; when the
configuration asks for it, the same measurement also produces the standby
qualification record (``qualify_standby``) whose parity challenge carries the
identical output digests and whose available bytes are the standby host's own
measurement.  A mismatch, a refusal or a missing observation seals nothing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

from mycelium_a7_standby_qualification import (
    StandbyQualificationError,
    parity_challenge_result,
    physical_parity_challenge,
    qualify_standby,
)

CONFIG_PROTOCOL = "mycelium.a7_parity_config.v1"
WORKER_PROTOCOL = "mycelium.a7_parity_worker.v1"
ROLES = ("entry", "source", "standby")
_CONFIG_FIELDS = frozenset(
    {"protocol", "source_node_id", "standby_node_id", "prompt_token_ids", "challenge_index", "workers", "max_checkpoint_bytes", "worker_timeout_s", "qualification"}
)
_QUALIFICATION_FIELDS = frozenset({"out", "reserved_kv_bytes", "ttl_ms"})


class ParityRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_config(document: Any) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != _CONFIG_FIELDS or document["protocol"] != CONFIG_PROTOCOL:
        raise ParityRefused("a7_parity_config_invalid")
    config = dict(document)
    workers = config["workers"]
    if not isinstance(workers, Mapping) or set(workers) != set(ROLES) or not all(
        isinstance(argv, list) and argv and all(isinstance(part, str) and part for part in argv) for argv in workers.values()
    ):
        raise ParityRefused("a7_parity_config_workers_invalid")
    prompt = config["prompt_token_ids"]
    if not isinstance(prompt, list) or not prompt or not all(type(token) is int and token >= 0 for token in prompt):
        raise ParityRefused("a7_parity_config_prompt_invalid")
    for name in ("challenge_index", "max_checkpoint_bytes"):
        if type(config[name]) is not int or config[name] < (0 if name == "challenge_index" else 1):
            raise ParityRefused("a7_parity_config_invalid")
    if not all(isinstance(config[name], str) and config[name] for name in ("source_node_id", "standby_node_id")):
        raise ParityRefused("a7_parity_config_invalid")
    if config["source_node_id"] == config["standby_node_id"]:
        raise ParityRefused("a7_parity_same_host")
    qualification = config["qualification"]
    if qualification is not None and (
        not isinstance(qualification, Mapping)
        or set(qualification) != _QUALIFICATION_FIELDS
        or not isinstance(qualification["out"], str)
        or type(qualification["reserved_kv_bytes"]) is not int
        or type(qualification["ttl_ms"]) is not int
    ):
        raise ParityRefused("a7_parity_config_qualification_invalid")
    if isinstance(config["worker_timeout_s"], bool) or not isinstance(config["worker_timeout_s"], (int, float)) or config["worker_timeout_s"] <= 0:
        raise ParityRefused("a7_parity_config_invalid")
    return config


class _Worker:
    def __init__(self, role: str, argv: Sequence[str], *, timeout_s: float) -> None:
        self.role = role
        self._timeout = timeout_s
        try:
            self._process = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as exc:
            raise ParityRefused(f"a7_parity_{role}_launch_failed") from exc
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, name=f"a7-parity-{role}", daemon=True).start()
        ready = self._read()
        if ready.get("ready") is not True:
            raise ParityRefused(f"a7_parity_{role}_not_ready:{ready.get('code', 'unknown')}")

    def _pump(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _read(self) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=self._timeout)
        except queue.Empty as exc:
            raise ParityRefused(f"a7_parity_{self.role}_unresponsive") from exc
        if line is None:
            raise ParityRefused(f"a7_parity_{self.role}_exited")
        try:
            document = json.loads(line)
        except ValueError as exc:
            raise ParityRefused(f"a7_parity_{self.role}_protocol_invalid") from exc
        if not isinstance(document, dict) or document.get("protocol") != WORKER_PROTOCOL:
            raise ParityRefused(f"a7_parity_{self.role}_protocol_invalid")
        return document

    def call(self, op: str, **fields: Any) -> dict[str, Any]:
        assert self._process.stdin is not None
        try:
            self._process.stdin.write(json.dumps({"op": op, **fields}) + "\n")
            self._process.stdin.flush()
        except OSError as exc:
            raise ParityRefused(f"a7_parity_{self.role}_unresponsive") from exc
        answer = self._read()
        if answer.get("ok") is not True:
            raise ParityRefused(f"a7_parity_{self.role}_{op}_refused:{answer.get('code', 'unknown')}")
        return answer

    def close(self) -> None:
        try:
            if self._process.poll() is None:
                self.call("close")
        except ParityRefused:
            pass
        finally:
            if self._process.poll() is None:
                self._process.kill()
            self._process.wait(timeout=30)


def _output_bytes(answer: Mapping[str, Any]) -> bytes:
    """The exact execute(...) output of one placement (payload and token), canonically."""

    return json.dumps({"payload_b64": answer.get("payload_b64"), "token_id": answer.get("token_id")}, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def measure_parity(config: Mapping[str, Any], *, candidate_commit: str, clock_ms: Any = None) -> dict[str, Any]:
    """Run one cross-host challenge; returns the sealed document and optional qualification."""

    from mycelium_router.payloads import encode_token_ids

    config = validate_config(config)
    now_ms = clock_ms or (lambda: int(time.time() * 1000))
    timeout = float(config["worker_timeout_s"])
    workers: dict[str, _Worker] = {}
    try:
        for role in ROLES:
            workers[role] = _Worker(role, config["workers"][role], timeout_s=timeout)
        entry, source, standby = (workers[role] for role in ROLES)
        prompt = tuple(config["prompt_token_ids"])
        position = len(prompt)
        hidden0 = entry.call("prefill", path_id="a7q:src", payload_b64=base64.b64encode(encode_token_ids(prompt)).decode("ascii"), span=position)
        first = source.call("prefill", path_id="a7q:src", payload_b64=hidden0["payload_b64"], span=position)
        if type(first.get("token_id")) is not int:
            raise ParityRefused("a7_parity_source_first_token_missing")
        checkpoint = source.call("export", path_id="a7q:src")
        step = base64.b64encode(encode_token_ids((first["token_id"],))).decode("ascii")
        hidden1 = entry.call("decode", path_id="a7q:src", path_attempt=0, payload_b64=step, position=position)
        source_out = source.call("decode", path_id="a7q:src", path_attempt=0, payload_b64=hidden1["payload_b64"], position=position)
        standby.call(
            "import",
            checkpoint_b64=checkpoint["checkpoint_b64"],
            path_id="a7q:sb",
            path_attempt=1,
            expected_next_position=position,
            max_bytes=config["max_checkpoint_bytes"],
        )
        standby_out = standby.call("decode", path_id="a7q:sb", path_attempt=1, payload_b64=hidden1["payload_b64"], position=position)
        source_identity = source.call("identity")
        standby_identity = standby.call("identity")
        measured_at = now_ms()
    finally:
        for worker in workers.values():
            worker.close()

    source_bytes, standby_bytes = _output_bytes(source_out), _output_bytes(standby_out)
    challenge_id = f"a7-physical-{candidate_commit[:8]}-{config['challenge_index']}"
    observation = {
        "source_output_digest": _digest(source_bytes),
        "standby_output_digest": _digest(standby_bytes),
        "checkpoint_bytes": len(base64.b64decode(checkpoint["checkpoint_b64"])),
        "next_position": position,
        "standby_available_bytes": standby_identity.get("available_bytes"),
    }
    try:
        document = physical_parity_challenge(
            challenge_id=challenge_id,
            source_identity=source_identity["identity"],
            standby_identity=standby_identity["identity"],
            source_output_digest=observation["source_output_digest"],
            standby_output_digest=observation["standby_output_digest"],
            candidate_commit=candidate_commit,
            measured_at_unix_ms=measured_at,
        )
    except StandbyQualificationError as exc:
        raised = ParityRefused(exc.code)
        raised.observation = observation  # type: ignore[attr-defined]
        raise raised from exc
    qualification = None
    if config["qualification"] is not None:
        available = standby_identity.get("available_bytes")
        qualification = qualify_standby(
            source_identity=source_identity["identity"],
            standby_identity=standby_identity["identity"],
            parity_challenge=parity_challenge_result(
                challenge_id=challenge_id, next_position=position, source_output=source_bytes, standby_output=standby_bytes
            ),
            reserved_kv_bytes=config["qualification"]["reserved_kv_bytes"],
            available_bytes=available if type(available) is int else None,
            issued_at_unix_ms=measured_at,
            ttl_ms=config["qualification"]["ttl_ms"],
        )
    return {"document": document, "qualification": qualification, "observation": observation}


def measure_identity_qualification(config: Mapping[str, Any], *, clock_ms: Any = None) -> dict[str, Any]:
   """Qualify a standby from the two placements' measured KV identities only.

   For the incompatible gate case: the standby's identity differs from the
   source's, so no parity challenge can be meaningful; the qualifier records
   ``identity_incompatible`` from the identities both workers report.
   """

   config = validate_config(config)
   if config["qualification"] is None:
      raise ParityRefused("a7_parity_config_qualification_invalid")
   now_ms = clock_ms or (lambda: int(time.time() * 1000))
   timeout = float(config["worker_timeout_s"])
   workers: dict[str, _Worker] = {}
   try:
      for role in ("source", "standby"):
         workers[role] = _Worker(role, config["workers"][role], timeout_s=timeout)
      source_identity = workers["source"].call("identity")
      standby_identity = workers["standby"].call("identity")
   finally:
      for worker in workers.values():
         worker.close()
   available = standby_identity.get("available_bytes")
   return qualify_standby(
      source_identity=source_identity["identity"],
      standby_identity=standby_identity["identity"],
      parity_challenge=None,
      reserved_kv_bytes=config["qualification"]["reserved_kv_bytes"],
      available_bytes=available if type(available) is int else None,
      issued_at_unix_ms=now_ms(),
      ttl_ms=config["qualification"]["ttl_ms"],
   )


__all__ = ("CONFIG_PROTOCOL", "ParityRefused", "measure_identity_qualification", "measure_parity", "validate_config")
