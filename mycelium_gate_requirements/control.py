# SPDX-License-Identifier: AGPL-3.0-only
"""Signed, gate-scoped control verbs for fault injection (replaces SSH kill/launchctl/adb).

The owner issues ``mycelium.gate_control_request.v1`` statements, signed with
the owner key, addressed to one bound role member (node id + generation) of
one verified role binding.  The member's OWN agent (``MemberControlAgent``)
executes a request only when all of these hold:

* the issuer key digest is the owner key the member pinned at join;
* the Ed25519 signature verifies over the canonical statement;
* the request names this node and its current generation;
* the member holds a standing gate grant for exactly this gate id and binding
  digest, the verb is in the grant, and the grant has not expired;
* the request is inside its validity window and its id was never seen.

It answers with ``mycelium.gate_control_receipt.v1``, signed by the member
key, naming the outcome (exit status for process verbs) and the request
digest - never a host name, address, user, path or process id.

``LocalProcessExecutor`` + ``LocalControlChannel`` are the local multi-process
implementation (real child processes on one host).  The member-side transport
over the managed control channel is NOT built yet; until it is, physical gate
scripts keep their SSH paths, marked legacy.
"""
from __future__ import annotations

import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .binding import verify_binding
from .spec import canonical_bytes, digest_of

REQUEST_PROTOCOL = "mycelium.gate_control_request.v1"
RECEIPT_PROTOCOL = "mycelium.gate_control_receipt.v1"
PUBLIC_RECEIPT_PROTOCOL = "mycelium.gate_control_receipt_public.v1"
VERBS = ("stop_process", "kill_process", "partition_link", "heal_link", "advance_generation")
MAX_REQUEST_TTL_SECONDS = 300.0
_CLOCK_SKEW_SECONDS = 30.0
_TARGET = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REQUEST_FIELDS = frozenset({"protocol", "gate_id", "binding_digest", "role", "node_id", "generation",
                             "verb", "target", "request_id", "issued_at", "expires_at",
                             "issuer_key_digest", "issuer_verification_key", "signature"})
_RECEIPT_FIELDS = frozenset({"protocol", "gate_id", "binding_digest", "role", "verb", "target",
                             "request_id", "request_digest", "generation", "outcome",
                             "completed_at", "member_key_digest", "member_verification_key",
                             "signature"})


class GateControlError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _verify_signature(statement: Mapping[str, Any], signature: Any, key_record: Any,
                      expected_key_digest: str) -> bool:
    from mycelium_qualification.signing import build_ed25519_verifier

    try:
        if not isinstance(key_record, Mapping) or key_record.get("verification_key_digest") != expected_key_digest:
            return False
        verify = build_ed25519_verifier([dict(key_record)])
        return bool(isinstance(signature, Mapping) and verify(canonical_bytes(dict(statement)), dict(signature)))
    except Exception:
        return False


def issue_control_request(*, owner: Any, binding: Mapping[str, Any], role: str, verb: str,
                          target: str, request_id: str, now: float, member_index: int = 0,
                          ttl_seconds: float = 60.0) -> dict[str, Any]:
    """Owner side: one signed request to the ``member_index``-th member bound to ``role``."""

    members = binding.get("roles", {}).get(role)
    if not members or not 0 <= member_index < len(members):
        raise GateControlError("control_role_unbound")
    if verb not in VERBS or not _TARGET.fullmatch(target) or not _REQUEST_ID.fullmatch(request_id):
        raise GateControlError("control_request_invalid")
    if not 0 < ttl_seconds <= MAX_REQUEST_TTL_SECONDS:
        raise GateControlError("control_request_invalid")
    member = members[member_index]
    statement = {
        "protocol": REQUEST_PROTOCOL, "gate_id": binding["gate_id"],
        "binding_digest": binding["binding_digest"], "role": role,
        "node_id": member["node_id"], "generation": member["generation"], "verb": verb,
        "target": target, "request_id": request_id, "issued_at": float(now),
        "expires_at": float(now) + float(ttl_seconds),
        "issuer_key_digest": owner.verification_key_digest,
        "issuer_verification_key": owner.public_key_record(),
    }
    return {**statement, "signature": owner.sign(statement)}


def request_digest(request: Mapping[str, Any]) -> str:
    return digest_of(dict(request))


@dataclass(frozen=True)
class ControlGrant:
    """The member's standing consent to one gate's fault verbs (revocable)."""

    gate_id: str
    binding_digest: str
    verbs: frozenset[str]
    expires_at: float


class LocalProcessExecutor:
    """Executes verbs against processes/links registered by name on this host."""

    def __init__(self, *, wait_seconds: float = 10.0) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._links: dict[str, Callable[[bool], None]] = {}
        self._wait_seconds = wait_seconds

    def register_process(self, target: str, process: subprocess.Popen) -> None:
        if not _TARGET.fullmatch(target):
            raise GateControlError("control_target_invalid")
        self._processes[target] = process

    def register_link(self, target: str, hook: Callable[[bool], None]) -> None:
        if not _TARGET.fullmatch(target):
            raise GateControlError("control_target_invalid")
        self._links[target] = hook

    def execute(self, verb: str, target: str) -> dict[str, Any]:
        if verb in {"stop_process", "kill_process"}:
            process = self._processes.get(target)
            if process is None:
                return {"state": "failed", "code": "control_target_unknown", "exit_status": None}
            if process.poll() is None:
                process.send_signal(signal.SIGTERM if verb == "stop_process" else signal.SIGKILL)
            try:
                status = process.wait(timeout=self._wait_seconds)
            except subprocess.TimeoutExpired:
                return {"state": "failed", "code": "control_process_did_not_exit", "exit_status": None}
            return {"state": "executed", "code": None, "exit_status": int(status)}
        if verb in {"partition_link", "heal_link"}:
            hook = self._links.get(target)
            if hook is None:
                return {"state": "failed", "code": "control_target_unknown", "exit_status": None}
            hook(verb == "heal_link")
            return {"state": "executed", "code": None, "exit_status": None}
        return {"state": "failed", "code": "control_verb_unsupported", "exit_status": None}


@dataclass
class MemberControlAgent:
    """Member side: verify, authorise and execute one signed control request."""

    node_id: str
    generation: int
    owner_key_digest: str
    grant: ControlGrant | None
    executor: Any
    signer: Any
    clock: Callable[[], float] = time.time
    _seen: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def handle(self, request: Any) -> dict[str, Any]:
        if (not isinstance(request, Mapping) or set(request) != _REQUEST_FIELDS
                or request.get("protocol") != REQUEST_PROTOCOL):
            raise GateControlError("control_request_invalid")
        if request["issuer_key_digest"] != self.owner_key_digest:
            raise GateControlError("control_request_issuer_untrusted")
        statement = {key: value for key, value in request.items() if key != "signature"}
        if not _verify_signature(statement, request["signature"], request["issuer_verification_key"],
                                 self.owner_key_digest):
            raise GateControlError("control_request_signature_invalid")
        if request["node_id"] != self.node_id or request["generation"] != self.generation:
            raise GateControlError("control_request_wrong_member")
        now = float(self.clock())
        if not (isinstance(request["issued_at"], float) and isinstance(request["expires_at"], float)
                and 0 < request["expires_at"] - request["issued_at"] <= MAX_REQUEST_TTL_SECONDS
                and request["issued_at"] - _CLOCK_SKEW_SECONDS <= now <= request["expires_at"]):
            raise GateControlError("control_request_expired")
        grant = self.grant
        if (grant is None or grant.gate_id != request["gate_id"]
                or grant.binding_digest != request["binding_digest"]):
            raise GateControlError("control_request_not_granted")
        if now > grant.expires_at:
            raise GateControlError("control_grant_expired")
        if request["verb"] not in grant.verbs or request["verb"] not in VERBS:
            raise GateControlError("control_verb_not_granted")
        digest = request_digest(request)
        with self._lock:
            if digest in self._seen or request["request_id"] in self._seen:
                raise GateControlError("control_request_replayed")
            self._seen.update({digest, request["request_id"]})
        outcome = self.executor.execute(request["verb"], request["target"])
        receipt = {
            "protocol": RECEIPT_PROTOCOL, "gate_id": request["gate_id"],
            "binding_digest": request["binding_digest"], "role": request["role"],
            "verb": request["verb"], "target": request["target"],
            "request_id": request["request_id"], "request_digest": digest,
            "generation": self.generation,
            "outcome": {"state": outcome["state"], "code": outcome["code"],
                        "exit_status": outcome["exit_status"]},
            "completed_at": float(self.clock()),
            "member_key_digest": self.signer.verification_key_digest,
            "member_verification_key": self.signer.public_key_record(),
        }
        return {**receipt, "signature": self.signer.sign(receipt)}


def verify_control_receipt(receipt: Any, *, request: Mapping[str, Any],
                           member_key_digest: str) -> dict[str, Any]:
    if (not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS
            or receipt.get("protocol") != RECEIPT_PROTOCOL):
        raise GateControlError("control_receipt_invalid")
    if receipt["member_key_digest"] != member_key_digest:
        raise GateControlError("control_receipt_untrusted")
    statement = {key: value for key, value in receipt.items() if key != "signature"}
    if not _verify_signature(statement, receipt["signature"], receipt["member_verification_key"],
                             member_key_digest):
        raise GateControlError("control_receipt_signature_invalid")
    if (receipt["request_digest"] != request_digest(request)
            or any(receipt[key] != request[key] for key in ("gate_id", "binding_digest", "role",
                                                            "verb", "target", "request_id"))):
        raise GateControlError("control_receipt_request_mismatch")
    return dict(receipt)


def public_control_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Host-free projection of a verified receipt for published gate evidence.

    The signed receipt stays private: its signature names the member's
    endpoint.  The projection keeps the outcome and binds the full receipt by
    digest, so a reviewer holding the private receipt can check it.
    """

    from .evidence import assert_host_free

    body = {key: receipt[key] for key in ("gate_id", "binding_digest", "role", "verb",
                                          "target", "request_digest", "outcome", "member_key_digest")}
    projection = {"protocol": PUBLIC_RECEIPT_PROTOCOL, **body,
                  "receipt_digest": digest_of(dict(receipt))}
    assert_host_free(projection)
    return projection


class LocalControlChannel:
    """Routes requests to member agents in this process (local multi-process gates)."""

    def __init__(self, agents: Mapping[str, MemberControlAgent]) -> None:
        self._agents = dict(agents)

    def send(self, request: Mapping[str, Any]) -> dict[str, Any]:
        agent = self._agents.get(request.get("node_id")) if isinstance(request, Mapping) else None
        if agent is None:
            raise GateControlError("control_member_unreachable")
        return agent.handle(request)


class SignedFaultInjector:
    """Owner-side helper: issue, send and verify one fault verb for a bound role."""

    def __init__(self, *, channel: Any, owner: Any, binding: Mapping[str, Any],
                 member_keys: Mapping[str, str], clock: Callable[[], float] = time.time) -> None:
        self._channel = channel
        self._owner = owner
        self._binding = verify_binding(binding)
        self._member_keys = dict(member_keys)
        self._clock = clock
        self._counter = 0

    @property
    def binding(self) -> Mapping[str, Any]:
        return self._binding

    def inject(self, *, role: str, verb: str, target: str, member_index: int = 0) -> dict[str, Any]:
        self._counter += 1
        request = issue_control_request(
            owner=self._owner, binding=self._binding, role=role, verb=verb, target=target,
            request_id=f"{self._binding['gate_id'].lower()}-{role}-{verb}-{self._counter}",
            now=float(self._clock()), member_index=member_index)
        receipt = self._channel.send(request)
        key = self._member_keys.get(request["node_id"])
        if key is None:
            raise GateControlError("control_member_key_unknown")
        return verify_control_receipt(receipt, request=request, member_key_digest=key)
