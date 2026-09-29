"""A13 Windows owner-only ACL verification through ``whoami`` + ``icacls`` (gap A4).

The listings below are hand-authored in ``icacls``'s documented display format
(first line prefixed by the path, continuation lines indented, a blank line,
then a summary).  They are NOT captured from a Windows device: the on-device
bring-up script (``scripts/a13_windows_device_bringup.ps1``) records real
``whoami``/``icacls`` output so these fixtures can be checked against the
device.  Every listing is fed through the same injectable runner the host
path uses, so the parser that runs on Windows is the parser tested here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_a13_windows import onboarding
from mycelium_a13_windows.acl import (
    NOT_OWNER_ONLY,
    OWNER_ONLY,
    AclVerificationError,
    make_icacls_acl_reader,
    parse_icacls_listing,
    parse_whoami,
    read_owner_only_acl,
)

PATH = "C:\\Users\\pilot\\AppData\\Local\\Mycelium\\state"
WHOAMI = "desktop-pilot\\pilot\r\n"
SUMMARY = "Successfully processed 1 files; Failed processing 0 files"

OWNER_ONLY_LISTING = (
    f"{PATH} NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\r\n"
    "                                          BUILTIN\\Administrators:(OI)(CI)(F)\r\n"
    "                                          DESKTOP-PILOT\\pilot:(OI)(CI)(F)\r\n"
    "\r\n"
    f"{SUMMARY}\r\n"
)
INHERITED_OWNER_ONLY_LISTING = (
    f"{PATH} DESKTOP-PILOT\\pilot:(I)(OI)(CI)(F)\n"
    "     NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)\n"
    "     BUILTIN\\Administrators:(I)(OI)(CI)(F)\n"
    "\n"
    f"{SUMMARY}\n"
)
USERS_READABLE_LISTING = (
    f"{PATH} NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n"
    "     BUILTIN\\Administrators:(OI)(CI)(F)\n"
    "     DESKTOP-PILOT\\pilot:(OI)(CI)(F)\n"
    "     BUILTIN\\Users:(OI)(CI)(RX)\n"
    "\n"
    f"{SUMMARY}\n"
)


def _runner(listing: str, *, whoami: str = WHOAMI, codes=(0, 0), calls=None):
    def run(argv):
        if calls is not None:
            calls.append(list(argv))
        if list(argv) == ["whoami"]:
            return codes[0], whoami
        assert list(argv) == ["icacls", PATH], argv
        return codes[1], listing

    return run


def _unverifiable(call) -> str:
    with pytest.raises(AclVerificationError) as caught:
        call()
    assert caught.value.code.startswith("acl_unverifiable:")
    return caught.value.code


@pytest.mark.parametrize(
    "listing",
    [
        OWNER_ONLY_LISTING,
        INHERITED_OWNER_ONLY_LISTING,
        # /Q-style output with no summary at all.
        OWNER_ONLY_LISTING.split("\r\n\r\n")[0] + "\r\n",
        # A localised summary line is tolerated after the blank line.
        OWNER_ONLY_LISTING.replace(SUMMARY, "1 Dateien erfolgreich verarbeitet; bei 0 Dateien ist ein Fehler aufgetreten"),
        # A DENY ACE grants nothing, so it never widens access.
        f"{PATH} Everyone:(OI)(CI)(DENY)(W)\n     DESKTOP-PILOT\\pilot:(OI)(CI)(F)\n\n{SUMMARY}\n",
        # Specific-rights groups are mixed case.
        f"{PATH} DESKTOP-PILOT\\pilot:(OI)(CI)(Rc,S,RD,WD)\n     NT AUTHORITY\\SYSTEM:(F)\n",
    ],
)
def test_owner_only_listings_are_recognised(listing) -> None:
    calls: list[list[str]] = []
    assert read_owner_only_acl(PATH, runner=_runner(listing, calls=calls)) == OWNER_ONLY
    assert calls == [["whoami"], ["icacls", PATH]]


@pytest.mark.parametrize(
    "extra",
    [
        "BUILTIN\\Users:(OI)(CI)(RX)",
        "Everyone:(R)",
        "NT AUTHORITY\\Authenticated Users:(OI)(CI)(M)",
        "CREATOR OWNER:(OI)(CI)(IO)(F)",
        "*S-1-5-21-1111111111-2222222222-3333333333-1001:(OI)(CI)(F)",
        "DESKTOP-PILOT\\other:(OI)(CI)(F)",
        "OTHER-PC\\pilot:(F)",
    ],
)
def test_any_other_granting_principal_is_not_owner_only(extra) -> None:
    listing = (
        f"{PATH} NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n"
        "     DESKTOP-PILOT\\pilot:(OI)(CI)(F)\n"
        f"     {extra}\n\n{SUMMARY}\n"
    )
    assert read_owner_only_acl(PATH, runner=_runner(listing)) == NOT_OWNER_ONLY


@pytest.mark.parametrize(
    ("listing", "reason"),
    [
        ("", "icacls_path_header_missing"),
        (f"C:\\Other\\path NT AUTHORITY\\SYSTEM:(F)\n", "icacls_path_header_missing"),
        (f"{PATH} NT AUTHORITY\\SYSTEM:(F)\n     garbage line\n", "icacls_ace_unparseable"),
        (f"{PATH} NT AUTHORITY\\SYSTEM:F\n", "icacls_ace_unparseable"),
        (f"{PATH}\n", "icacls_ace_unparseable"),
        (
            f"{PATH} DESKTOP-PILOT\\pilot:(F)\n\n     BUILTIN\\Users:(RX)\n",
            "icacls_ace_after_block",
        ),
        (f"{PATH} Everyone:(DENY)(F)\n", "icacls_no_granting_ace"),
    ],
)
def test_unreadable_listings_fail_closed(listing, reason) -> None:
    code = _unverifiable(lambda: read_owner_only_acl(PATH, runner=_runner(listing)))
    assert code == f"acl_unverifiable:{reason}"


def test_the_principal_comes_from_whoami_never_the_environment(monkeypatch) -> None:
    # A process that spoofs its environment cannot make BUILTIN\Users "the user".
    monkeypatch.setenv("USERDOMAIN", "BUILTIN")
    monkeypatch.setenv("USERNAME", "Users")
    assert read_owner_only_acl(PATH, runner=_runner(USERS_READABLE_LISTING)) == NOT_OWNER_ONLY
    for whoami in ("builtin\\users\n", "nt authority\\system\n", "nt service\\mycelium\n"):
        assert _unverifiable(
            lambda whoami=whoami: read_owner_only_acl(
                PATH, runner=_runner(OWNER_ONLY_LISTING, whoami=whoami)
            )
        ) == "acl_unverifiable:whoami_not_a_user"
    for whoami in ("", "pilot\n", "a\\b\nc\\d\n", "desk<top\\pilot\n"):
        assert _unverifiable(lambda whoami=whoami: parse_whoami(whoami)) == (
            "acl_unverifiable:whoami_output_invalid"
        )


def test_tool_failures_fail_closed() -> None:
    assert _unverifiable(
        lambda: read_owner_only_acl(PATH, runner=_runner(OWNER_ONLY_LISTING, codes=(1, 0)))
    ) == "acl_unverifiable:whoami_failed"
    assert _unverifiable(
        lambda: read_owner_only_acl(PATH, runner=_runner(OWNER_ONLY_LISTING, codes=(0, 5)))
    ) == "acl_unverifiable:icacls_failed"

    def broken(_argv):
        raise FileNotFoundError("icacls")

    assert _unverifiable(lambda: read_owner_only_acl(PATH, runner=broken)) == (
        "acl_unverifiable:runner_failed"
    )


def test_the_default_runner_exists_only_on_a_win32_host() -> None:
    assert _unverifiable(lambda: read_owner_only_acl(PATH, platform="darwin")) == (
        "acl_unverifiable:no_win32_host"
    )
    assert _unverifiable(lambda: read_owner_only_acl(PATH, platform="linux")) == (
        "acl_unverifiable:no_win32_host"
    )


def test_parse_icacls_listing_is_the_same_decision_without_a_runner() -> None:
    assert parse_icacls_listing(OWNER_ONLY_LISTING, path=PATH, principal="DESKTOP-PILOT\\pilot") == OWNER_ONLY
    assert parse_icacls_listing(USERS_READABLE_LISTING, path=PATH, principal="DESKTOP-PILOT\\pilot") == NOT_OWNER_ONLY


# ------------------------------------------- through the public join entry point


def _join_arguments(tmp_path: Path):
    from mycelium_invite import mint_invite_bundle
    from mycelium_qualification.signing import generate_ed25519_signer

    now = 3_000_000_000.0
    target = generate_ed25519_signer(endpoint_id="win-acl-target")
    seed = generate_ed25519_signer(endpoint_id="win-acl-seed")
    request, private_key = onboarding.new_windows_pairing_request(
        signer=target, node_id="mycelium-win-acl", now=now, ttl_seconds=300, nonce="pair-win-acl"
    )
    bundle = mint_invite_bundle(
        signer=seed,
        swarm_id="judge-swarm",
        seed_url="https://127.0.0.1:4443",
        ttl_seconds=120,
        nonce="pair-win-acl",
        issued_at=now,
    )
    handoff = onboarding.approve_windows_pairing_request(
        request=request,
        invite_bundle=bundle,
        quota={"storage_bytes": 1_000_000, "network_bytes_per_day": 1_000_000, "background_allowed": False},
        now=now,
    )
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    identity = root / "identity.key"
    identity.write_bytes(b"\x33" * 32)
    return {
        "state_root": root,
        "identity_path": identity,
        "request": request,
        "handoff": handoff,
        "recipient_private_key": private_key,
        "seed_key_digest_pin": seed.verification_key_digest,
        "member_generation": 1,
        "acl_observation": "owner_only",
        "directory_flush": lambda directory: None,
        "now": now + 5,
    }


def _listing_runner(listing_for):
    def run(argv):
        if list(argv) == ["whoami"]:
            return 0, WHOAMI
        assert argv[0] == "icacls" and len(argv) == 2
        return 0, listing_for(argv[1])

    return run


def test_the_join_accepts_an_owner_only_listing_on_a_simulated_win32_host(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    arguments = _join_arguments(tmp_path)
    reader = make_icacls_acl_reader(
        _listing_runner(
            lambda path: f"{path} NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n     DESKTOP-PILOT\\pilot:(OI)(CI)(F)\n\n{SUMMARY}\n"
        )
    )
    result = onboarding.complete_local_join(**arguments, acl_reader=reader)
    assert result["joined"] is True


def test_the_join_refuses_a_users_readable_state_root_on_a_simulated_win32_host(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    arguments = _join_arguments(tmp_path)
    reader = make_icacls_acl_reader(
        _listing_runner(
            lambda path: f"{path} DESKTOP-PILOT\\pilot:(OI)(CI)(F)\n     BUILTIN\\Users:(OI)(CI)(RX)\n\n{SUMMARY}\n"
        )
    )
    with pytest.raises(onboarding.OnboardingError) as caught:
        onboarding.complete_local_join(**arguments, acl_reader=reader)
    assert caught.value.code == "onboarding_state_root_unsafe"
    assert not (arguments["state_root"] / "onboarding" / "member.json").exists()


def test_the_join_refuses_an_unparseable_listing_on_a_simulated_win32_host(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "win32")
    arguments = _join_arguments(tmp_path)
    reader = make_icacls_acl_reader(_listing_runner(lambda path: "Access is denied.\n"))
    with pytest.raises(onboarding.OnboardingError) as caught:
        onboarding.complete_local_join(**arguments, acl_reader=reader)
    assert caught.value.code == "onboarding_acl_unverifiable"


def test_the_default_onboarding_reader_still_refuses_off_a_win32_host() -> None:
    with pytest.raises(onboarding.OnboardingError) as caught:
        onboarding._default_win32_acl_reader(Path(PATH))
    assert caught.value.code == "onboarding_acl_unverifiable"
