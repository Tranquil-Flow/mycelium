"""Thread-safe capability-scoped browser worker coordinator."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import secrets
import struct
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

from mycelium_invite import InviteError, SqliteInviteRegistry
from mycelium_node import NodeMembershipSession
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator, SeedCoordinatorError, SqliteSeedState

WORK_PROTOCOL = "mycelium.browser_stage_work.v1"
RESULT_PROTOCOL = "mycelium.browser_stage_result.v1"
STATUS_PROTOCOL = "mycelium.interactive_status.v1"
_RESULT_FIELDS = frozenset(
    {
        "protocol",
        "job_id",
        "request_id",
        "assignment_id",
        "stage_id",
        "pack_digest",
        "input_digest",
        "output",
        "output_digest",
        "route_ready",
    }
)
_MAX_SEQUENCE_LENGTH = 256
_MAX_LONG_POLL_SECONDS = 25.0
_BROWSER_ELIGIBLE_LIFECYCLE_STATES = frozenset({"NEW", "CONFIGURED", "RUNNING"})


class SwarmError(ValueError):
    """Fail-closed coordinator error carrying one stable public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> NoReturn:
    raise SwarmError(code)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _reject("noncanonical_document")


def canonical_digest(value: Any) -> str:
    """Return the Pixel-stage compatible canonical JSON digest."""

    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def matrix_digest(value: Any) -> str:
    """Hash a finite rectangular matrix with a cross-language binary encoding."""

    if not isinstance(value, (list, tuple)) or not value:
        _reject("matrix_digest_invalid")
    rows = len(value)
    first = value[0]
    if not isinstance(first, (list, tuple)) or not first:
        _reject("matrix_digest_invalid")
    columns = len(first)
    if rows > _MAX_SEQUENCE_LENGTH or columns > 4096:
        _reject("matrix_digest_invalid")
    payload = bytearray(b"mycelium.matrix.f64be.v1\x00")
    payload.extend(struct.pack(">II", rows, columns))
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != columns:
            _reject("matrix_digest_invalid")
        for item in row:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                _reject("matrix_digest_invalid")
            number = float(item)
            if not math.isfinite(number):
                _reject("matrix_digest_invalid")
            payload.extend(struct.pack(">d", number))
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _token_digest(token: str) -> str:
    if not isinstance(token, str) or not 16 <= len(token) <= 4096:
        _reject("credential_invalid")
    try:
        raw = token.encode("ascii")
    except UnicodeEncodeError:
        _reject("credential_invalid")
    return hashlib.sha256(raw).hexdigest()


def _copy_json(value: Any) -> Any:
    return json.loads(_canonical_bytes(value).decode("ascii"))


def _positive_finite(value: Any, code: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(code)
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or (not allow_zero and number == 0.0):
        _reject(code)
    return number


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        _reject(code)
    if any(ord(character) < 0x20 for character in value):
        _reject(code)
    return value


def normalize_public_origin(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        _reject("public_origin_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        _reject("public_origin_invalid")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.netloc.endswith(":")
        or port == 0
    ):
        _reject("public_origin_invalid")
    hostname = parsed.hostname
    if not hostname:
        _reject("public_origin_invalid")
    loopback = hostname.casefold() == "localhost" or hostname in {"127.0.0.1", "::1"}
    if parsed.scheme != "https" and not loopback:
        _reject("public_origin_invalid")
    return value.rstrip("/")


def _validate_stage_pack(stage_pack: Any) -> dict[str, Any]:
    if not isinstance(stage_pack, Mapping):
        _reject("stage_pack_invalid")
    required = {
        "protocol",
        "assignment_id",
        "stage_id",
        "start_layer",
        "end_layer_exclusive",
        "hidden_size",
        "pack_digest",
        "route_ready",
        "tensors",
    }
    if not required.issubset(stage_pack):
        _reject("stage_pack_invalid")
    if stage_pack.get("protocol") != "mycelium.pixel_stage_pack.v1":
        _reject("stage_pack_invalid")
    if stage_pack.get("route_ready") is not False:
        _reject("stage_pack_invalid")
    start = stage_pack.get("start_layer")
    end = stage_pack.get("end_layer_exclusive")
    hidden_size = stage_pack.get("hidden_size")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or start < 0
        or isinstance(end, bool)
        or not isinstance(end, int)
        or end != start + 1
        or isinstance(hidden_size, bool)
        or not isinstance(hidden_size, int)
        or not 1 <= hidden_size <= 4096
    ):
        _reject("stage_pack_invalid")
    _identifier(stage_pack.get("assignment_id"), "stage_pack_invalid")
    _identifier(stage_pack.get("stage_id"), "stage_pack_invalid")
    pack_digest = stage_pack.get("pack_digest")
    if (
        not isinstance(pack_digest, str)
        or len(pack_digest) != 71
        or not pack_digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in pack_digest[7:])
    ):
        _reject("stage_pack_invalid")
    return _copy_json(dict(stage_pack))


def _validate_hidden(value: Any, hidden_size: int) -> list[list[float]]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_SEQUENCE_LENGTH:
        _reject("hidden_invalid")
    rows: list[list[float]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != hidden_size:
            _reject("hidden_invalid")
        normalized: list[float] = []
        for item in row:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                _reject("hidden_invalid")
            number = float(item)
            if not math.isfinite(number):
                _reject("hidden_invalid")
            normalized.append(number)
        rows.append(normalized)
    return rows


@dataclass(frozen=True, slots=True)
class Invitation:
    token: str
    url: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class JoinGrant:
    peer_id: str
    session_token: str
    expires_at: float
    stage_pack: dict[str, Any]
    membership_acceptance: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BrowserStageResult:
    peer_id: str
    job_id: str
    request_id: str
    output: tuple[tuple[float, ...], ...]
    output_digest: str


@dataclass(slots=True)
class _Invite:
    digest: str
    expires_at: float
    nonce: str


@dataclass(slots=True)
class _Peer:
    peer_id: str
    token_digest: str | None
    membership_generation: int
    created_at: float
    expires_at: float
    last_seen_at: float
    state: str = "connected"
    outstanding_job_id: str | None = None
    completed_jobs: int = 0


@dataclass(slots=True)
class _Job:
    job_id: str
    request_id: str
    hidden: list[list[float]]
    input_digest: str
    created_at: float
    excluded_peer_ids: frozenset[str] = frozenset()
    allowed_peer_ids: frozenset[str] | None = None
    cancel_event: threading.Event | None = None
    state: str = "pending"
    peer_id: str | None = None
    membership_generation: int | None = None
    result: BrowserStageResult | None = None
    result_document_digest: str | None = None


class SwarmCoordinator:
    """Coordinate one-time browser enrollment and exact-stage work delivery."""

    def __init__(
        self,
        *,
        stage_pack: Mapping[str, Any],
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        seed_coordinator: SeedCoordinator | None = None,
        token_source: Callable[[], str] | None = None,
        id_source: Callable[[str], str] | None = None,
        max_peers: int = 16,
        invite_ttl_seconds: float = 300.0,
        session_ttl_seconds: float = 3_600.0,
        peer_idle_ttl_seconds: float = 45.0,
        max_pending_jobs: int = 64,
        max_peer_history: int = 64,
        max_job_history: int = 256,
    ) -> None:
        if (
            not callable(clock)
            or not callable(wall_clock)
            or not callable(token_source or secrets.token_urlsafe)
        ):
            raise TypeError("clocks and token source must be callable")
        if not callable(id_source or (lambda _prefix: "")):
            raise TypeError("id source must be callable")
        if type(max_peers) is not int or not 1 <= max_peers <= 1_024:
            _reject("max_peers_invalid")
        if type(max_pending_jobs) is not int or not 1 <= max_pending_jobs <= 4_096:
            _reject("max_pending_jobs_invalid")
        if (
            type(max_peer_history) is not int
            or not max_peers <= max_peer_history <= 4_096
        ):
            _reject("max_peer_history_invalid")
        if (
            type(max_job_history) is not int
            or not max_pending_jobs <= max_job_history <= 65_536
        ):
            _reject("max_job_history_invalid")
        self._stage_pack = _validate_stage_pack(stage_pack)
        self._hidden_size = int(self._stage_pack["hidden_size"])
        self._clock = clock
        self._wall_clock = wall_clock
        self._token_source = token_source or (lambda: secrets.token_urlsafe(48))
        self._id_source = id_source or (lambda prefix: f"{prefix}-{secrets.token_urlsafe(18)}")
        self._max_peers = max_peers
        self._max_peer_history = max_peer_history
        self._invite_ttl = _positive_finite(invite_ttl_seconds, "invite_ttl_invalid")
        self._session_ttl = _positive_finite(session_ttl_seconds, "session_ttl_invalid")
        self._peer_idle_ttl = _positive_finite(
            peer_idle_ttl_seconds, "peer_idle_ttl_invalid"
        )
        if self._peer_idle_ttl > self._session_ttl:
            _reject("peer_idle_ttl_invalid")
        self._max_pending_jobs = max_pending_jobs
        self._max_job_history = max_job_history
        if seed_coordinator is not None and not isinstance(
            seed_coordinator,
            SeedCoordinator,
        ):
            raise TypeError("seed coordinator must be a SeedCoordinator")
        self._membership_tempdir: tempfile.TemporaryDirectory[str] | None = None
        if seed_coordinator is None:
            self._membership_tempdir = tempfile.TemporaryDirectory(
                prefix="mycelium-browser-membership-",
                dir=Path(tempfile.gettempdir()).resolve(),
            )
            database = self._membership_tempdir.name + "/seed-state.sqlite3"
            seed_coordinator = SeedCoordinator(
                swarm_id="interactive-swarm",
                seed_node_id="interactive-seed",
                seed_url="https://interactive.invalid",
                signer=generate_ed25519_signer(
                    endpoint_id="interactive-seed-endpoint"
                ),
                invite_registry=SqliteInviteRegistry(database),
                state=SqliteSeedState(database),
                incarnation="interactive-seed-incarnation",
                clock=self._wall_clock,
                lease_seconds=min(self._session_ttl, 3_600.0),
                message_ttl_seconds=min(self._session_ttl, 60.0),
            )
        self._seed = seed_coordinator
        self._condition = threading.Condition(threading.RLock())
        self._invites: dict[str, _Invite] = {}
        self._peers: OrderedDict[str, _Peer] = OrderedDict()
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._poll_waiters: dict[str, int] = {}
        self._restore_browser_members()

    def __repr__(self) -> str:
        with self._condition:
            return (
                f"SwarmCoordinator(peers={len(self._peers)}, invites={len(self._invites)}, "
                f"jobs={len(self._jobs)}, route_ready=False)"
            )

    def _now(self) -> float:
        try:
            now = self._clock()
        except Exception:
            _reject("clock_unavailable")
        return _positive_finite(now, "clock_invalid", allow_zero=True)

    def _wall_now(self) -> float:
        try:
            now = self._wall_clock()
        except Exception:
            _reject("wall_clock_unavailable")
        return _positive_finite(now, "wall_clock_invalid", allow_zero=True)

    @staticmethod
    def _browser_state(member: Mapping[str, Any], *, wall_now: float) -> str:
        lifecycle_state = member.get("lifecycle_state")
        if lifecycle_state == "STOPPED":
            return "left"
        if lifecycle_state not in _BROWSER_ELIGIBLE_LIFECYCLE_STATES:
            return "revoked"
        if wall_now >= float(member["lease_expires_at"]):
            return "expired"
        return "connected"

    def _restore_browser_members(self) -> None:
        now = self._now()
        wall_now = self._wall_now()
        try:
            members = self._seed.members(peer_class="browser_http")
        except SeedCoordinatorError:
            _reject("membership_state_unavailable")
        for member in members:
            remaining_lease = max(
                0.0,
                float(member["lease_expires_at"]) - wall_now,
            )
            self._peers[member["node_id"]] = _Peer(
                peer_id=member["node_id"],
                token_digest=None,
                membership_generation=int(member["generation"]),
                created_at=now,
                expires_at=now + min(remaining_lease, self._session_ttl),
                last_seen_at=now,
                state=self._browser_state(member, wall_now=wall_now),
            )

    def _new_token(self) -> tuple[str, str]:
        for _ in range(8):
            try:
                token = self._token_source()
            except Exception:
                _reject("token_source_unavailable")
            digest = _token_digest(token)
            if digest not in self._invites and all(
                peer.token_digest is None
                or not hmac.compare_digest(digest, peer.token_digest)
                for peer in self._peers.values()
            ):
                return token, digest
        _reject("token_collision")

    def _new_id(self, prefix: str) -> str:
        try:
            value = self._id_source(prefix)
        except Exception:
            _reject("id_source_unavailable")
        return _identifier(value, "id_source_invalid")

    def _expire_locked(self, now: float) -> None:
        for digest, invite in tuple(self._invites.items()):
            if now >= invite.expires_at:
                self._invites.pop(digest, None)
        for peer in self._peers.values():
            if peer.state == "connected" and (
                now >= peer.expires_at or now - peer.last_seen_at >= self._peer_idle_ttl
            ):
                peer.state = "expired"
                self._fail_peer_job_locked(peer, "peer_unavailable")

    def _prune_peer_history_locked(self) -> None:
        for peer_id, peer in tuple(self._peers.items()):
            if len(self._peers) < self._max_peer_history:
                return
            if peer.state != "connected":
                self._peers.pop(peer_id, None)

    def _prune_job_history_locked(self) -> None:
        for job_id, job in tuple(self._jobs.items()):
            if len(self._jobs) < self._max_job_history:
                return
            if job.state not in {"pending", "assigned", "running"}:
                self._jobs.pop(job_id, None)

    def create_invite(
        self,
        *,
        public_origin: str,
        ttl_seconds: float | None = None,
    ) -> Invitation:
        origin = normalize_public_origin(public_origin)
        ttl = self._invite_ttl if ttl_seconds is None else _positive_finite(
            ttl_seconds, "invite_ttl_invalid"
        )
        if ttl > self._invite_ttl:
            _reject("invite_ttl_invalid")
        public_expires_at = self._wall_now() + ttl
        with self._condition:
            now = self._now()
            self._expire_locked(now)
            nonce = f"browser-{secrets.token_hex(16)}"
            try:
                bundle = self._seed.mint_invite(
                    nonce=nonce,
                    ttl_seconds=max(1, math.ceil(ttl)),
                )
                token = bundle["token"]
                digest = _token_digest(token)
            except (
                InviteError,
                SeedCoordinatorError,
                KeyError,
                TypeError,
                ValueError,
            ):
                _reject("invite_mint_failed")
            if digest in self._invites:
                _reject("token_collision")
            deadline = now + ttl
            self._invites[digest] = _Invite(
                digest=digest,
                expires_at=deadline,
                nonce=nonce,
            )
            return Invitation(
                token=token,
                url=f"{origin}/#join/{token}",
                expires_at=public_expires_at,
            )

    def exchange_invite(self, token: str) -> JoinGrant:
        try:
            digest = _token_digest(token)
        except SwarmError:
            _reject("invite_invalid_or_consumed")
        with self._condition:
            now = self._now()
            self._expire_locked(now)
            invite = self._invites.get(digest)
            if invite is None or not hmac.compare_digest(invite.digest, digest):
                _reject("invite_invalid_or_consumed")
            public_expires_at = self._wall_now() + self._session_ttl
            self._invites.pop(digest, None)
            active_peers = sum(peer.state == "connected" for peer in self._peers.values())
            if active_peers >= self._max_peers:
                _reject("peer_capacity_exhausted")
            self._prune_peer_history_locked()
            peer_id = self._new_id("peer")
            if peer_id in self._peers:
                _reject("id_collision")
            browser_signer = generate_ed25519_signer(
                endpoint_id=(
                    "browser-http-"
                    + hashlib.sha256(peer_id.encode("utf-8")).hexdigest()[:32]
                )
            )
            try:
                membership = NodeMembershipSession(
                    node_id=peer_id,
                    swarm_id=self._seed.swarm_id,
                    seed_node_id=self._seed.seed_node_id,
                    signer=browser_signer,
                    incarnation=(
                        "browser-"
                        + hashlib.sha256(
                            f"{peer_id}:{digest}".encode("utf-8")
                        ).hexdigest()[:32]
                    ),
                    software_version="mycelium-interactive",
                    peer_class="browser_http",
                    runtime_capability={
                        "runtime_backend": "browser",
                        "transport": "http",
                        "activation_protocol": None,
                    },
                    clock=self._wall_clock,
                    id_source=lambda: f"browser-message-{secrets.token_hex(16)}",
                )
                request = membership.join_request(
                    invite_nonce=invite.nonce,
                    endpoint_addrs=["https://browser.invalid/control"],
                )
                acceptance = self._seed.accept_join(
                    invite_token=token,
                    join_envelope=request,
                )
            except SeedCoordinatorError as exc:
                if exc.code in {
                    "seed_member_identity_reused",
                    "seed_node_endpoint_conflict",
                    "seed_node_key_conflict",
                }:
                    _reject("id_collision")
                _reject("membership_join_failed")
            except (InviteError, TypeError, ValueError):
                _reject("membership_join_failed")
            session_token, session_digest = self._new_token()
            generation = int(acceptance["message"]["membership_generation"])
            expires_at = now + self._session_ttl
            self._peers[peer_id] = _Peer(
                peer_id=peer_id,
                token_digest=session_digest,
                membership_generation=generation,
                created_at=now,
                expires_at=expires_at,
                last_seen_at=now,
            )
            self._condition.notify_all()
            return JoinGrant(
                peer_id=peer_id,
                session_token=session_token,
                expires_at=public_expires_at,
                stage_pack=_copy_json(self._stage_pack),
                membership_acceptance=_copy_json(acceptance),
            )

    def _authenticate_peer_locked(
        self,
        *,
        peer_id: str,
        session_token: str,
        now: float,
        permit_left: bool = False,
    ) -> _Peer:
        peer = self._peers.get(peer_id)
        if peer is None:
            _reject("peer_unauthorized")
        try:
            supplied = _token_digest(session_token)
        except SwarmError:
            _reject("peer_unauthorized")
        if peer.token_digest is None or not hmac.compare_digest(
            peer.token_digest,
            supplied,
        ):
            _reject("peer_unauthorized")
        if peer.state == "revoked":
            _reject("peer_revoked")
        if peer.state == "expired":
            _reject("peer_expired")
        if peer.state == "left" and not permit_left:
            _reject("peer_left")
        self._require_current_membership_locked(peer)
        peer.last_seen_at = now
        return peer

    def _require_current_membership_locked(self, peer: _Peer) -> dict[str, Any]:
        try:
            with self._seed.member_authority_guard(
                node_id=peer.peer_id,
                expected_generation=peer.membership_generation,
                expected_peer_class="browser_http",
                eligible_lifecycle_states=_BROWSER_ELIGIBLE_LIFECYCLE_STATES,
            ) as member:
                return member
        except SeedCoordinatorError as exc:
            self._reject_authority_error_locked(peer, exc)

    def _reject_authority_error_locked(
        self,
        peer: _Peer,
        error: SeedCoordinatorError,
    ) -> NoReturn:
        if error.code == "seed_member_unknown":
            _reject("peer_membership_unknown")
        if error.code == "seed_member_lease_expired":
            peer.state = "expired"
            self._fail_peer_job_locked(peer, "peer_unavailable")
            _reject("peer_expired")
        if error.code == "seed_member_lifecycle_ineligible":
            peer.state = "revoked"
            self._fail_peer_job_locked(peer, "peer_unavailable")
            _reject("peer_revoked")
        if error.code in {
            "seed_member_generation_stale",
            "seed_member_peer_class_mismatch",
            "seed_state_member_stale",
        }:
            _reject("peer_membership_generation_revoked")
        _reject("membership_state_unavailable")

    def poll_work(
        self,
        *,
        peer_id: str,
        session_token: str,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        timeout = _positive_finite(timeout_seconds, "poll_timeout_invalid", allow_zero=True)
        if timeout > _MAX_LONG_POLL_SECONDS:
            _reject("poll_timeout_invalid")
        with self._condition:
            now = self._now()
            deadline = now + timeout
            self._expire_locked(now)
            peer = self._authenticate_peer_locked(
                peer_id=peer_id,
                session_token=session_token,
                now=now,
            )
            waiter_peer_id = peer.peer_id
            self._poll_waiters[waiter_peer_id] = self._poll_waiters.get(waiter_peer_id, 0) + 1
            self._condition.notify_all()
            try:
                while True:
                    self._expire_locked(now)
                    peer = self._authenticate_peer_locked(
                        peer_id=peer_id,
                        session_token=session_token,
                        now=now,
                    )
                    if peer.outstanding_job_id is not None:
                        existing = self._jobs.get(peer.outstanding_job_id)
                        if existing is not None and existing.state in {"assigned", "running"}:
                            return self._work_document(existing)
                        peer.outstanding_job_id = None
                    pending = next(
                        (job for job in self._jobs.values() if job.state == "pending"),
                        None,
                    )
                    if pending is not None:
                        eligible = [
                            candidate
                            for candidate in self._peers.values()
                            if candidate.state == "connected"
                            and candidate.peer_id not in pending.excluded_peer_ids
                            and (
                                pending.allowed_peer_ids is None
                                or candidate.peer_id in pending.allowed_peer_ids
                            )
                            and candidate.outstanding_job_id is None
                            and self._poll_waiters.get(candidate.peer_id, 0) > 0
                        ]
                        selected = min(
                            eligible,
                            key=lambda candidate: (candidate.completed_jobs, candidate.peer_id),
                            default=None,
                        )
                        if selected is not None and selected.peer_id == peer.peer_id:
                            pending.state = "assigned"
                            pending.peer_id = peer.peer_id
                            pending.membership_generation = peer.membership_generation
                            peer.outstanding_job_id = pending.job_id
                            self._condition.notify_all()
                            return self._work_document(pending)
                    remaining = deadline - now
                    if remaining <= 0.0:
                        return None
                    self._condition.wait(timeout=remaining)
                    now = self._now()
            finally:
                waiter_count = self._poll_waiters.get(waiter_peer_id, 0)
                if waiter_count <= 1:
                    self._poll_waiters.pop(waiter_peer_id, None)
                else:
                    self._poll_waiters[waiter_peer_id] = waiter_count - 1
                self._condition.notify_all()

    def _work_document(self, job: _Job) -> dict[str, Any]:
        return {
            "protocol": WORK_PROTOCOL,
            "job_id": job.job_id,
            "request_id": job.request_id,
            "assignment_id": self._stage_pack["assignment_id"],
            "stage_id": self._stage_pack["stage_id"],
            "pack_digest": self._stage_pack["pack_digest"],
            "input_digest": job.input_digest,
            "hidden": _copy_json(job.hidden),
            "route_ready": False,
        }

    def start_work(
        self,
        *,
        peer_id: str,
        session_token: str,
        job_id: str,
        request_id: str,
        input_digest: str,
    ) -> bool:
        """Atomically permit browser compute only while its assignment remains active."""

        job_id = _identifier(job_id, "work_start_job_invalid")
        request_id = _identifier(request_id, "work_start_binding_invalid")
        input_digest = _identifier(input_digest, "work_start_binding_invalid")
        with self._condition:
            now = self._now()
            self._expire_locked(now)
            peer = self._authenticate_peer_locked(
                peer_id=peer_id,
                session_token=session_token,
                now=now,
            )
            job = self._jobs.get(job_id)
            if job is None or job.peer_id != peer.peer_id:
                _reject("work_start_job_mismatch")
            if job.membership_generation != peer.membership_generation:
                _reject("peer_membership_generation_revoked")
            if job.request_id != request_id or job.input_digest != input_digest:
                _reject("work_start_binding_invalid")
            if job.cancel_event is not None and job.cancel_event.is_set():
                self._cancel_job_locked(job, "cancelled")
                return False
            if job.state == "running" and peer.outstanding_job_id == job.job_id:
                return True
            if job.state != "assigned" or peer.outstanding_job_id != job.job_id:
                return False
            job.state = "running"
            self._condition.notify_all()
            return True

    def dispatch(
        self,
        *,
        request_id: str,
        hidden: Any,
        timeout_seconds: float,
        excluded_peer_ids: set[str] | frozenset[str] | None = None,
        allowed_peer_ids: set[str] | frozenset[str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> BrowserStageResult:
        request_id = _identifier(request_id, "request_id_invalid")
        rows = _validate_hidden(hidden, self._hidden_size)
        timeout = _positive_finite(timeout_seconds, "dispatch_timeout_invalid", allow_zero=True)
        if excluded_peer_ids is None:
            excluded = frozenset()
        elif not isinstance(excluded_peer_ids, (set, frozenset)):
            _reject("excluded_peer_ids_invalid")
        else:
            excluded = frozenset(
                _identifier(peer_id, "excluded_peer_ids_invalid")
                for peer_id in excluded_peer_ids
            )
            if len(excluded) > self._max_peers:
                _reject("excluded_peer_ids_invalid")
        if allowed_peer_ids is None:
            allowed = None
        elif not isinstance(allowed_peer_ids, (set, frozenset)):
            _reject("allowed_peer_ids_invalid")
        else:
            allowed = frozenset(
                _identifier(peer_id, "allowed_peer_ids_invalid")
                for peer_id in allowed_peer_ids
            )
            if not allowed or len(allowed) > self._max_peers:
                _reject("allowed_peer_ids_invalid")
        if allowed is not None and allowed.intersection(excluded):
            _reject("peer_eligibility_invalid")
        if timeout > 300.0:
            _reject("dispatch_timeout_invalid")
        with self._condition:
            if cancel_event is not None and cancel_event.is_set():
                _reject("request_cancelled")
            now = self._now()
            self._expire_locked(now)
            active_jobs = sum(
                job.state in {"pending", "assigned", "running"}
                for job in self._jobs.values()
            )
            if active_jobs >= self._max_pending_jobs:
                _reject("job_capacity_exhausted")
            self._prune_job_history_locked()
            if any(
                job.request_id == request_id
                and job.state in {"pending", "assigned", "running"}
                for job in self._jobs.values()
            ):
                _reject("request_already_active")
            job_id = self._new_id("job")
            if job_id in self._jobs:
                _reject("id_collision")
            job = _Job(
                job_id=job_id,
                request_id=request_id,
                hidden=rows,
                input_digest=matrix_digest(rows),
                created_at=now,
                excluded_peer_ids=excluded,
                allowed_peer_ids=allowed,
                cancel_event=cancel_event,
            )
            self._jobs[job_id] = job
            if cancel_event is not None and cancel_event.is_set():
                self._cancel_job_locked(job, "cancelled")
                _reject("request_cancelled")
            self._condition.notify_all()
            deadline = now + timeout
            while True:
                if job.state == "completed" and job.result is not None:
                    return job.result
                if job.state == "cancelled":
                    _reject("request_cancelled")
                if job.state == "peer_unavailable":
                    _reject("peer_unavailable")
                if cancel_event is not None and cancel_event.is_set():
                    self._cancel_job_locked(job, "cancelled")
                    _reject("request_cancelled")
                remaining = deadline - self._now()
                if cancel_event is not None and cancel_event.is_set():
                    self._cancel_job_locked(job, "cancelled")
                    _reject("request_cancelled")
                if remaining <= 0.0:
                    self._cancel_job_locked(job, "cancelled")
                    _reject("dispatch_timeout")
                self._condition.wait(timeout=remaining)

    def submit_result(
        self,
        *,
        peer_id: str,
        session_token: str,
        document: Any,
    ) -> str:
        if (
            not isinstance(document, Mapping)
            or set(document) != set(_RESULT_FIELDS)
            or document.get("protocol") != RESULT_PROTOCOL
        ):
            _reject("result_fields_or_protocol_invalid")
        job_id = _identifier(document.get("job_id"), "result_job_mismatch")
        with self._condition:
            now = self._now()
            self._expire_locked(now)
            peer = self._authenticate_peer_locked(
                peer_id=peer_id,
                session_token=session_token,
                now=now,
            )
            job = self._jobs.get(job_id)
            if job is None or job.peer_id != peer.peer_id:
                _reject("result_job_mismatch")
            if job.membership_generation != peer.membership_generation:
                _reject("peer_membership_generation_revoked")
            if (
                document.get("request_id") != job.request_id
                or document.get("assignment_id") != self._stage_pack["assignment_id"]
                or document.get("stage_id") != self._stage_pack["stage_id"]
                or document.get("pack_digest") != self._stage_pack["pack_digest"]
                or document.get("input_digest") != job.input_digest
            ):
                _reject("result_binding_mismatch")
            if document.get("route_ready") is not False:
                _reject("result_route_ready_invalid")
            try:
                output = _validate_hidden(document.get("output"), self._hidden_size)
            except SwarmError:
                _reject("result_output_invalid")
            if len(output) != len(job.hidden):
                _reject("result_output_invalid")
            expected_digest = matrix_digest(output)
            if document.get("output_digest") != expected_digest:
                _reject("result_output_digest_mismatch")
            document_digest = canonical_digest(dict(document))
            try:
                # Global lock order is adapter condition -> seed authority ->
                # durable state write reservation. Neither seed layer acquires
                # this condition or calls back into the adapter.
                with self._seed.member_authority_guard(
                    node_id=peer.peer_id,
                    expected_generation=peer.membership_generation,
                    expected_peer_class="browser_http",
                    eligible_lifecycle_states=_BROWSER_ELIGIBLE_LIFECYCLE_STATES,
                ):
                    if job.state == "completed":
                        if job.result_document_digest == document_digest:
                            return "duplicate"
                        _reject("result_replay_conflict")
                    if job.cancel_event is not None and job.cancel_event.is_set():
                        self._cancel_job_locked(job, "cancelled")
                        _reject("result_job_not_active")
                    if (
                        job.state != "running"
                        or peer.outstanding_job_id != job.job_id
                    ):
                        _reject("result_job_not_active")
                    result = BrowserStageResult(
                        peer_id=peer.peer_id,
                        job_id=job.job_id,
                        request_id=job.request_id,
                        output=tuple(tuple(row) for row in output),
                        output_digest=expected_digest,
                    )
                    job.result = result
                    job.result_document_digest = document_digest
                    job.state = "completed"
                    peer.outstanding_job_id = None
                    peer.completed_jobs += 1
                    self._condition.notify_all()
                    return "accepted"
            except SeedCoordinatorError as exc:
                self._reject_authority_error_locked(peer, exc)

    def cancel_request(
        self,
        request_id: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        request_id = _identifier(request_id, "request_id_invalid")
        with self._condition:
            for job in reversed(tuple(self._jobs.values())):
                if (
                    job.request_id == request_id
                    and job.state in {"pending", "assigned", "running"}
                ):
                    if cancel_event is not None:
                        cancel_event.set()
                    self._cancel_job_locked(job, "cancelled")
                    return True
            if cancel_event is not None and not any(
                job.request_id == request_id for job in self._jobs.values()
            ):
                # Runtime published this stage id before coordinator job creation.
                # Reserve cancellation under the same coordinator lock so dispatch
                # observes the event before it can enqueue browser work.
                cancel_event.set()
                self._condition.notify_all()
                return True
            return False

    def _cancel_job_locked(self, job: _Job, state: str) -> None:
        if job.state not in {"pending", "assigned", "running"}:
            return
        job.state = state
        job.result = None
        job.result_document_digest = None
        if job.peer_id is not None:
            peer = self._peers.get(job.peer_id)
            if peer is not None and peer.outstanding_job_id == job.job_id:
                peer.outstanding_job_id = None
        self._condition.notify_all()

    def _fail_peer_job_locked(self, peer: _Peer, state: str) -> None:
        if peer.outstanding_job_id is None:
            return
        job = self._jobs.get(peer.outstanding_job_id)
        if job is not None and job.state in {"assigned", "running"}:
            job.state = state
        peer.outstanding_job_id = None
        self._condition.notify_all()

    def _advance_membership_locked(
        self,
        peer: _Peer,
        *,
        lifecycle_state: str,
    ) -> None:
        try:
            self._seed.advance_member_generation(
                node_id=peer.peer_id,
                expected_generation=peer.membership_generation,
                lifecycle_state=lifecycle_state,
            )
        except SeedCoordinatorError as exc:
            if exc.code == "seed_member_generation_stale":
                _reject("peer_membership_generation_revoked")
            _reject("membership_state_unavailable")

    def revoke_peer(self, peer_id: str) -> bool:
        peer_id = _identifier(peer_id, "peer_id_invalid")
        with self._condition:
            peer = self._peers.get(peer_id)
            if peer is None or peer.state == "revoked":
                return False
            self._advance_membership_locked(peer, lifecycle_state="STOPPING")
            peer.state = "revoked"
            self._fail_peer_job_locked(peer, "peer_unavailable")
            self._condition.notify_all()
            return True

    def leave(self, *, peer_id: str, session_token: str) -> bool:
        with self._condition:
            now = self._now()
            self._expire_locked(now)
            peer = self._peers.get(peer_id)
            if peer is None:
                _reject("peer_unauthorized")
            try:
                supplied = _token_digest(session_token)
            except SwarmError:
                _reject("peer_unauthorized")
            if peer.token_digest is None or not hmac.compare_digest(
                peer.token_digest,
                supplied,
            ):
                _reject("peer_unauthorized")
            if peer.state == "left":
                return False
            if peer.state == "revoked":
                _reject("peer_revoked")
            if peer.state == "expired":
                _reject("peer_expired")
            self._require_current_membership_locked(peer)
            self._advance_membership_locked(peer, lifecycle_state="STOPPED")
            peer.state = "left"
            self._fail_peer_job_locked(peer, "peer_unavailable")
            self._condition.notify_all()
            return True

    def status(self) -> dict[str, Any]:
        with self._condition:
            now = self._now()
            self._expire_locked(now)
            wall_now = self._wall_now()
            peers = []
            for peer in self._peers.values():
                try:
                    member = self._seed.member(peer.peer_id)
                except SeedCoordinatorError:
                    _reject("membership_state_unavailable")
                authority_state = self._browser_state(member, wall_now=wall_now)
                state = peer.state
                if authority_state != "connected":
                    state = authority_state
                elif (
                    int(member["generation"]) != peer.membership_generation
                    and state == "connected"
                ):
                    state = "revoked"
                peers.append(
                    {
                        "peer_id": peer.peer_id,
                        "state": state,
                        "completed_jobs": peer.completed_jobs,
                        "peer_class": member["peer_class"],
                        "activation_eligible": member["activation_eligible"],
                        "membership_generation": int(member["generation"]),
                        "assigned_layer": {
                            "start_layer": self._stage_pack["start_layer"],
                            "end_layer_exclusive": self._stage_pack[
                                "end_layer_exclusive"
                            ],
                        },
                        "pack_digest": self._stage_pack["pack_digest"],
                    }
                )
            return {
                "protocol": STATUS_PROTOCOL,
                "local_evidence_only": True,
                "route_ready": False,
                "peer_count": sum(peer["state"] == "connected" for peer in peers),
                "ready_peer_count": sum(
                    item["state"] == "connected"
                    and self._peers[item["peer_id"]].outstanding_job_id is None
                    and self._poll_waiters.get(item["peer_id"], 0) > 0
                    for item in peers
                ),
                "pending_job_count": sum(
                    job.state in {"pending", "assigned", "running"}
                    for job in self._jobs.values()
                ),
                "peers": peers,
            }

    def debug_storage(self) -> dict[str, Any]:
        """Expose digest-only internals for executable secret-storage assertions."""

        with self._condition:
            return {
                "invite_digests": sorted(self._invites),
                "peer_token_digests": sorted(
                    peer.token_digest
                    for peer in self._peers.values()
                    if peer.token_digest is not None
                ),
                "job_ids": list(self._jobs),
            }
