# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-only NTFS ACL verification for the Windows state root (gap A4).

The Windows local join refuses to trust a caller-supplied ``owner_only`` string
on win32: the observation is re-derived from the host.  This module is that
derivation.  It asks the host who the signed-in user is (``whoami``) and what
the path's DACL grants (``icacls <path>``), and reduces the listing to one of
``owner_only`` / ``not_owner_only``, raising :class:`AclVerificationError`
(fail closed) whenever either answer cannot be read unambiguously.

Both commands go through one injectable *runner* (``argv -> (exit code,
stdout)``), so the parser is exercised on any host against recorded ``icacls``
listings.  The default runner exists only on an actual win32 host; elsewhere
it refuses.  The principal comes from ``whoami``, never from the
``USERDOMAIN``/``USERNAME`` environment, which the calling process controls.

Grammar accepted (anything else is unverifiable):

* the first non-empty line starts with the path exactly as passed to icacls;
* every line of the ACE block is ``PRINCIPAL:(flag)(flag)...``, where each flag
  group is letters and commas (``(OI)``, ``(CI)``, ``(I)``, ``(F)``,
  ``(DENY)``, ``(Rc,S,RD)``, ...);
* a blank line ends the ACE block; trailing summary lines (localised) are
  ignored only if they cannot be mistaken for an ACE (no ``:(``).

A ``(DENY)`` ACE grants nothing and is ignored; every *granting* ACE must name
the signed-in user, ``NT AUTHORITY\\SYSTEM`` or ``BUILTIN\\Administrators``.
Unresolved SIDs (``*S-1-...``), ``Everyone``, ``BUILTIN\\Users``,
``Authenticated Users``, ``CREATOR OWNER`` and any other principal make the
path ``not_owner_only``.  This is conservative: it may call a genuinely
private path "not owner only", never the reverse.  Never exercised on a real
Windows device by this program.

Native verification (the default on a win32 host)
-------------------------------------------------

``icacls`` prints localised principal names (``NT-AUTORITÄT\\SYSTEM`` on a
German host), so the text parser alone would refuse a genuinely private path
on a non-English Windows.  :func:`read_owner_only_acl_native` therefore
decides from the security descriptor itself: it opens the path without
following reparse points, reads the owner and DACL through
``GetSecurityInfo`` (via :mod:`mycelium_host_platform`) and compares SIDs,
which are locale-independent.  Owner-only there means: owned by the token
user; a non-NULL DACL; every ACE an access-allowed ACE for the token user,
SYSTEM or Administrators; at least one ACE for the user.  Deny, object and
unknown ACE types are refused rather than interpreted.

:func:`read_owner_only_acl_cross_checked` is what the Windows join uses by
default: the native verdict decides, and ``icacls`` is kept as an
independent cross-check of the binding -- its ACE count must equal the
native ACE count, otherwise the observation is unverifiable (fail closed).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import re
import sys
from typing import Any

OWNER_ONLY = "owner_only"
NOT_OWNER_ONLY = "not_owner_only"
TRUSTED_SYSTEM_PRINCIPALS: frozenset[str] = frozenset(
    {"NT AUTHORITY\\SYSTEM", "BUILTIN\\ADMINISTRATORS"}
)
#: Domains whose accounts are groups, services or the machine -- never a user.
_NON_USER_DOMAINS: tuple[str, ...] = (
    "NT AUTHORITY\\",
    "BUILTIN\\",
    "NT SERVICE\\",
    "NT VIRTUAL MACHINE\\",
)
_WHOAMI = re.compile(r"^[^\\/:*?\"<>|\s][^\\/:*?\"<>|]{0,62}\\[^\\/:*?\"<>|\s][^\\/:*?\"<>|]{0,63}$")
_ACE = re.compile(r"^(?P<principal>[^:()]+?):(?P<flags>(?:\([A-Za-z,]+\))+)$")
_FLAG = re.compile(r"\(([A-Za-z,]+)\)")
_TIMEOUT_SECONDS = 10

#: ``argv -> (exit code, decoded stdout)``.
AclRunner = Callable[[Sequence[str]], tuple[int, str]]


class AclVerificationError(ValueError):
    """The ACL could not be verified; carries a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _unverifiable(reason: str) -> AclVerificationError:
    return AclVerificationError(f"acl_unverifiable:{reason}")


def parse_whoami(output: object) -> str:
    """The signed-in ``DOMAIN\\user``, upper-cased as icacls comparisons need."""

    if not isinstance(output, str):
        raise _unverifiable("whoami_output_invalid")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1 or _WHOAMI.fullmatch(lines[0]) is None:
        raise _unverifiable("whoami_output_invalid")
    principal = lines[0].upper()
    if principal.startswith(_NON_USER_DOMAINS):
        raise _unverifiable("whoami_not_a_user")
    return principal


def parse_icacls_listing(output: object, *, path: str, principal: str) -> str:
    """Reduce one ``icacls <path>`` listing to ``owner_only``/``not_owner_only``."""

    if not isinstance(output, str) or not isinstance(path, str) or not path:
        raise _unverifiable("icacls_output_invalid")
    user = parse_whoami(principal)
    lines = output.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or not lines[0].startswith(path):
        raise _unverifiable("icacls_path_header_missing")
    block: list[str] = [lines[0][len(path) :].strip()]
    rest = lines[1:]
    for index, raw in enumerate(rest):
        if not raw.strip():
            trailer = rest[index + 1 :]
            if any(":(" in item for item in trailer):
                raise _unverifiable("icacls_ace_after_block")
            break
        block.append(raw.strip())
    grants: set[str] = set()
    for line in block:
        match = _ACE.fullmatch(line)
        if match is None:
            raise _unverifiable("icacls_ace_unparseable")
        flags = {
            item
            for group in _FLAG.findall(match.group("flags"))
            for item in group.split(",")
        }
        if "DENY" in flags:
            continue
        grants.add(match.group("principal").strip().upper())
    if not grants:
        raise _unverifiable("icacls_no_granting_ace")
    if grants - (TRUSTED_SYSTEM_PRINCIPALS | {user}):
        return NOT_OWNER_ONLY
    return OWNER_ONLY


def count_icacls_aces(output: object, *, path: str) -> int:
    """Number of ACE lines in one ``icacls <path>`` listing (locale-independent).

    Uses the same header/block grammar as :func:`parse_icacls_listing` but no
    principal names, so it can be compared with a native ACE count on any
    display language.
    """

    if not isinstance(output, str) or not isinstance(path, str) or not path:
        raise _unverifiable("icacls_output_invalid")
    lines = output.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or not lines[0].startswith(path):
        raise _unverifiable("icacls_path_header_missing")
    block: list[str] = [lines[0][len(path) :].strip()]
    rest = lines[1:]
    for index, raw in enumerate(rest):
        if not raw.strip():
            if any(":(" in item for item in rest[index + 1 :]):
                raise _unverifiable("icacls_ace_after_block")
            break
        block.append(raw.strip())
    for line in block:
        if _ACE.fullmatch(line) is None:
            raise _unverifiable("icacls_ace_unparseable")
    return len(block)


def _host_runner(argv: Sequence[str]) -> tuple[int, str]:
    """Run one read-only platform tool; only reachable on an actual win32 host."""

    import subprocess

    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    return completed.returncode, completed.stdout


def read_owner_only_acl(
    path: Path | str,
    *,
    runner: AclRunner | None = None,
    platform: str | None = None,
) -> str:
    """Derive the owner-only observation for ``path`` from the host, fail closed."""

    if runner is None:
        if (sys.platform if platform is None else platform) != "win32":
            raise _unverifiable("no_win32_host")
        runner = _host_runner
    target = str(path)
    try:
        who_code, who_output = runner(["whoami"])
        user = parse_whoami(who_output) if who_code == 0 else None
        if user is None:
            raise _unverifiable("whoami_failed")
        acl_code, acl_output = runner(["icacls", target])
    except AclVerificationError:
        raise
    except Exception as exc:  # noqa: BLE001 - external tool boundary
        raise _unverifiable("runner_failed") from exc
    if acl_code != 0:
        raise _unverifiable("icacls_failed")
    return parse_icacls_listing(acl_output, path=target, principal=user)


def _native_descriptor(path: Path | str, api: Any) -> tuple[Any, str]:
    """Owner/DACL of ``path`` and the token user SID, read from an open handle."""

    from mycelium_host_platform import _win32_api as w

    try:
        user_sid = api.current_user_sid()
        handle = api.open(
            str(path),
            access=w.READ_CONTROL | w.FILE_READ_ATTRIBUTES,
            share=w.FILE_SHARE_READ | w.FILE_SHARE_WRITE | w.FILE_SHARE_DELETE,
            disposition=w.OPEN_EXISTING,
            flags=w.FILE_FLAG_BACKUP_SEMANTICS | w.FILE_FLAG_OPEN_REPARSE_POINT,
        )
    except w.Win32Error as exc:
        raise _unverifiable("native_open_failed") from exc
    try:
        information = api.information(handle)
        if information.attributes & w.FILE_ATTRIBUTE_REPARSE_POINT:
            raise _unverifiable("native_reparse_point")
        descriptor = api.security(handle)
    except w.Win32Error as exc:
        raise _unverifiable("native_security_unreadable") from exc
    finally:
        try:
            api.close(handle)
        except w.Win32Error:
            pass
    if not isinstance(user_sid, str) or not user_sid.startswith("S-1-"):
        raise _unverifiable("native_token_user_invalid")
    return descriptor, user_sid


def _native_api(api: Any, platform: str | None) -> Any:
    if api is not None:
        return api
    if (sys.platform if platform is None else platform) != "win32":
        raise _unverifiable("no_win32_host")
    import mycelium_host_platform

    try:
        return mycelium_host_platform.win32_api()
    except mycelium_host_platform.HostPlatformError as exc:
        raise _unverifiable("native_api_unavailable") from exc


def _native_verdict(descriptor: Any, user_sid: str) -> str:
    from mycelium_host_platform import _win32_api as w
    from mycelium_host_platform._win32 import owner_only_violation

    reason = owner_only_violation(
        descriptor,
        user_sid,
        trusted_sids=(w.SYSTEM_SID, w.ADMINISTRATORS_SID),
        require_protected=False,
    )
    return OWNER_ONLY if reason is None else NOT_OWNER_ONLY


def read_owner_only_acl_native(
    path: Path | str,
    *,
    api: Any = None,
    platform: str | None = None,
) -> str:
    """Owner-only observation from the security descriptor itself (SIDs)."""

    descriptor, user_sid = _native_descriptor(path, _native_api(api, platform))
    return _native_verdict(descriptor, user_sid)


def read_owner_only_acl_cross_checked(
    path: Path | str,
    *,
    api: Any = None,
    runner: AclRunner | None = None,
    platform: str | None = None,
) -> str:
    """Native verdict, cross-checked against ``icacls`` by ACE count."""

    native_api = _native_api(api, platform)
    if runner is None:
        if (sys.platform if platform is None else platform) != "win32":
            raise _unverifiable("no_win32_host")
        runner = _host_runner
    descriptor, user_sid = _native_descriptor(path, native_api)
    verdict = _native_verdict(descriptor, user_sid)
    target = str(path)
    try:
        acl_code, acl_output = runner(["icacls", target])
    except Exception as exc:  # noqa: BLE001 - external tool boundary
        raise _unverifiable("runner_failed") from exc
    if acl_code != 0:
        raise _unverifiable("icacls_failed")
    listed = count_icacls_aces(acl_output, path=target)
    native_count = 0 if descriptor.aces is None else len(descriptor.aces)
    if descriptor.aces is None or listed != native_count:
        raise _unverifiable("native_icacls_disagree")
    return verdict


def make_icacls_acl_reader(
    runner: AclRunner | None = None, *, platform: str | None = None
) -> Callable[[Path], str]:
    """An ``acl_reader`` for :func:`mycelium_a13_windows.onboarding.complete_local_join`."""

    def reader(path: Path) -> str:
        return read_owner_only_acl(path, runner=runner, platform=platform)

    return reader


def make_native_acl_reader(
    *,
    api: Any = None,
    runner: AclRunner | None = None,
    platform: str | None = None,
) -> Callable[[Path], str]:
    """The default Windows join ``acl_reader``: native verdict + icacls cross-check."""

    def reader(path: Path) -> str:
        return read_owner_only_acl_cross_checked(
            path, api=api, runner=runner, platform=platform
        )

    return reader


__all__ = [
    "NOT_OWNER_ONLY",
    "OWNER_ONLY",
    "TRUSTED_SYSTEM_PRINCIPALS",
    "AclRunner",
    "AclVerificationError",
    "count_icacls_aces",
    "make_icacls_acl_reader",
    "make_native_acl_reader",
    "parse_icacls_listing",
    "parse_whoami",
    "read_owner_only_acl",
    "read_owner_only_acl_cross_checked",
    "read_owner_only_acl_native",
]
