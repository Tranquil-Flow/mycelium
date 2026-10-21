# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Windows join-path platform seams (gaps A4 and A6, cross-lane FINDINGS §3.4).

Gap A4: the durable state root / identity ACL was a caller-asserted string on
win32 (no real check).  Gap A6: the atomic join write used POSIX-only
primitives (``os.O_NOFOLLOW``, a directory ``fsync``) with no win32 path at
all.

Neither gap can be exercised on a real Windows host from here, so every test
below drives the win32 branch by monkeypatching ``onboarding.sys.platform``
and, where the real OS call cannot run on macOS (``icacls``, ``ctypes.windll``),
injecting a fake reader/flush through the seam the production code also
exposes on the public ``complete_local_join`` entry point.  These tests prove
two things: (1) the win32 path is now real verification, not a rubber stamp
of the caller string, and (2) it fails CLOSED, never open, when it cannot
verify.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_a13_windows import onboarding


def _state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    return root


def _code(call):
    with pytest.raises(onboarding.OnboardingError) as caught:
        call()
    return caught.value.code


# --------------------------------------------------------------- gap A4: ACL


def test_win32_private_directory_rejects_caller_string_alone(tmp_path, monkeypatch) -> None:
    """A bare caller-asserted ``owner_only`` string is not enough on win32.

    No reader is injected, and there is no real ``icacls`` on this host, so
    the default win32 reader must fail CLOSED rather than fall back to
    trusting ``acl_observation``.
    """

    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    root = _state_root(tmp_path)
    assert (
        _code(
            lambda: onboarding._private_directory(
                root, create=False, acl_observation="owner_only"
            )
        )
        == "onboarding_acl_unverifiable"
    )


def test_win32_private_directory_honours_an_injected_owner_only_reader(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    root = _state_root(tmp_path)
    result = onboarding._private_directory(
        root,
        create=False,
        acl_observation="owner_only",
        acl_reader=lambda path: "owner_only",
    )
    assert result == root


def test_win32_private_directory_rejects_when_the_reader_observes_not_owner_only(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    root = _state_root(tmp_path)
    assert (
        _code(
            lambda: onboarding._private_directory(
                root,
                create=False,
                acl_observation="owner_only",
                acl_reader=lambda path: "not_owner_only",
            )
        )
        == "onboarding_state_root_unsafe"
    )


def test_win32_checked_identity_honours_an_injected_reader(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    identity = tmp_path / "identity.key"
    identity.write_bytes(b"\x22" * 32)
    result = onboarding._checked_identity(
        identity, acl_observation="owner_only", acl_reader=lambda path: "owner_only"
    )
    assert result == identity


def test_win32_acl_reader_fails_closed_on_garbage_output(tmp_path, monkeypatch) -> None:
    """A reader returning a value outside ``ACL_OBSERVATIONS`` is never trusted."""

    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    root = _state_root(tmp_path)
    assert (
        _code(
            lambda: onboarding._private_directory(
                root,
                create=False,
                acl_observation="owner_only",
                acl_reader=lambda path: "definitely_not_a_real_observation",
            )
        )
        == "onboarding_acl_unverifiable"
    )


def test_win32_acl_reader_exception_is_converted_never_propagated_raw(
    tmp_path, monkeypatch
) -> None:
    """A reader that blows up fails closed with a stable code, not a raw traceback."""

    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    root = _state_root(tmp_path)

    def _boom(path: Path) -> str:
        raise RuntimeError("simulated icacls failure")

    assert (
        _code(
            lambda: onboarding._private_directory(
                root, create=False, acl_observation="owner_only", acl_reader=_boom
            )
        )
        == "onboarding_acl_unverifiable"
    )


def test_default_win32_acl_reader_refuses_off_a_real_win32_host() -> None:
    """The real reader is only reachable on an actual win32 host, by construction."""

    assert onboarding.sys.platform != "win32"
    assert (
        _code(lambda: onboarding._default_win32_acl_reader(Path("/does/not/matter")))
        == "onboarding_acl_unverifiable"
    )


# ------------------------------------------------------- gap A6: atomic write


def test_win32_atomic_write_uses_the_injected_directory_flush(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    target = tmp_path / "member.json"
    flushed: list[Path] = []
    onboarding._atomic_write(
        target, b'{"ok": true}', directory_flush=flushed.append
    )
    assert target.read_bytes() == b'{"ok": true}'
    assert flushed == [tmp_path]
    # The temporary file is always cleaned up, win32 or not.
    assert not (tmp_path / ("." + target.name + ".joining")).exists()


def test_win32_atomic_write_rejects_a_symlinked_temp_path(tmp_path, monkeypatch) -> None:
    """Missing ``O_NOFOLLOW`` on win32 is substituted by an explicit pre-check."""

    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    target = tmp_path / "member.json"
    temporary = tmp_path / ("." + target.name + ".joining")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"not the join file")
    temporary.symlink_to(elsewhere)
    assert (
        _code(
            lambda: onboarding._atomic_write(
                target, b"data", directory_flush=lambda directory: None
            )
        )
        == "onboarding_join_write_unsafe"
    )
    # The attack target must be untouched.
    assert elsewhere.read_bytes() == b"not the join file"


def test_win32_atomic_write_fails_closed_without_a_real_host_or_injected_flush(
    tmp_path, monkeypatch
) -> None:
    """No real Windows host and nothing injected: never silently skip durability."""

    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    target = tmp_path / "member.json"
    assert (
        _code(lambda: onboarding._atomic_write(target, b"data"))
        == "onboarding_join_durability_unavailable"
    )
    # The write must not be left claiming success: the file may exist (the
    # rename already happened) but the caller receives a hard failure, never
    # a silent, unverified durability claim.


def test_win32_atomic_write_flush_exception_is_converted_never_propagated_raw(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    target = tmp_path / "member.json"

    def _boom(directory: Path) -> None:
        raise OSError("simulated FlushFileBuffers failure")

    assert (
        _code(lambda: onboarding._atomic_write(target, b"data", directory_flush=_boom))
        == "onboarding_join_durability_unavailable"
    )


def test_posix_atomic_write_is_unchanged_by_the_win32_seam(tmp_path) -> None:
    """No platform simulation: the exact original POSIX primitives still run."""

    target = tmp_path / "member.json"
    onboarding._atomic_write(target, b'{"posix": true}')
    assert target.read_bytes() == b'{"posix": true}'


def test_default_win32_flush_directory_refuses_off_a_real_win32_host(tmp_path) -> None:
    assert onboarding.sys.platform != "win32"
    assert (
        _code(lambda: onboarding._win32_flush_directory(tmp_path))
        == "onboarding_join_durability_unavailable"
    )


# --------------------------------------------------- end-to-end: complete_local_join


def test_complete_local_join_end_to_end_on_a_simulated_win32_host(tmp_path, monkeypatch) -> None:
    """The full public entry point succeeds on a simulated win32 host once both

    seams are supplied — proving gaps A4 and A6 are closed at the API callers
    actually use, not only at the private helpers.
    """

    from mycelium_invite import mint_invite_bundle
    from mycelium_qualification.signing import generate_ed25519_signer

    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    now = 3_000_000_000.0
    target = generate_ed25519_signer(endpoint_id="win-e2e-target")
    seed = generate_ed25519_signer(endpoint_id="win-e2e-seed")
    request, private_key = onboarding.new_windows_pairing_request(
        signer=target, node_id="mycelium-win-e2e", now=now, ttl_seconds=300, nonce="pair-win-e2e"
    )
    bundle = mint_invite_bundle(
        signer=seed,
        swarm_id="judge-swarm",
        seed_url="https://127.0.0.1:4443",
        ttl_seconds=120,
        nonce="pair-win-e2e",
        issued_at=now,
    )
    handoff = onboarding.approve_windows_pairing_request(
        request=request, invite_bundle=bundle, quota={
            "storage_bytes": 1_000_000,
            "network_bytes_per_day": 1_000_000,
            "background_allowed": False,
        }, now=now
    )
    root = _state_root(tmp_path)
    identity = tmp_path / "identity.key"
    identity.write_bytes(b"\x33" * 32)

    result = onboarding.complete_local_join(
        state_root=root,
        identity_path=identity,
        request=request,
        handoff=handoff,
        recipient_private_key=private_key,
        seed_key_digest_pin=seed.verification_key_digest,
        member_generation=1,
        acl_observation="owner_only",
        acl_reader=lambda path: "owner_only",
        directory_flush=lambda directory: None,
        now=now + 5,
    )
    assert result["joined"] is True
    assert (root / "onboarding" / "member.json").exists()


def test_complete_local_join_fails_closed_on_win32_when_acl_cannot_be_verified(
    tmp_path, monkeypatch
) -> None:
    """Without an injected reader (and no real ``icacls`` here), the join refuses."""

    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    root = _state_root(tmp_path)
    identity = tmp_path / "identity.key"
    identity.write_bytes(b"\x44" * 32)
    assert (
        _code(
            lambda: onboarding.complete_local_join(
                state_root=root,
                identity_path=identity,
                request={},
                handoff={},
                recipient_private_key=b"\x00" * 32,
                seed_key_digest_pin="sha256:" + "0" * 64,
                member_generation=1,
                acl_observation="owner_only",
                now=1.0,
            )
        )
        == "onboarding_acl_unverifiable"
    )
