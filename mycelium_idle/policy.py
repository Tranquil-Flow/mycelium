# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner/member idle-unload setting: minutes of idleness before weights are dropped, or 'never'."""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

POLICY_FILENAME: Final = "idle-unload-policy.json"
DEFAULT_MINUTES: Final = 10
MIN_MINUTES: Final = 1
MAX_MINUTES: Final = 1440
NEVER: Final = "never"
_MAX_BYTES: Final = 1024
_KEYS: Final = frozenset({"schema", "unload_after_minutes"})


class IdlePolicyError(ValueError):
    """Raised with one bounded, content-free ``code``."""

    def __init__(self, code: str = "idle_policy_invalid") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class IdlePolicy:
    unload_after_minutes: int | None  # None = never

    @property
    def unload_after_s(self) -> float | None:
        return None if self.unload_after_minutes is None else self.unload_after_minutes * 60.0

    @property
    def setting(self) -> int | str:
        return NEVER if self.unload_after_minutes is None else self.unload_after_minutes


def parse_setting(raw: Any) -> int | None:
    """``int`` minutes (1..1440, also as a digit string) or ``'never'`` (-> ``None``)."""
    if type(raw) is str:
        if raw == NEVER:
            return None
        if not raw.isascii() or not raw.isdigit():
            raise IdlePolicyError()
        raw = int(raw)
    if type(raw) is not int or not MIN_MINUTES <= raw <= MAX_MINUTES:
        raise IdlePolicyError()
    return raw


def default_policy() -> IdlePolicy:
    return IdlePolicy(DEFAULT_MINUTES)


def read_policy(root: str | os.PathLike[str]) -> IdlePolicy:
    """The stored policy; a missing file is the default.  Anything unreadable raises."""
    path = Path(root) / POLICY_FILENAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return default_policy()
    except OSError as error:
        raise IdlePolicyError() from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > _MAX_BYTES):
            raise IdlePolicyError()
        raw = os.read(fd, _MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise IdlePolicyError() from error
    if not isinstance(value, dict) or set(value) != _KEYS or value["schema"] != 1 or type(value["schema"]) is not int:
        raise IdlePolicyError()
    return IdlePolicy(parse_setting(value["unload_after_minutes"]))


def write_policy(root: str | os.PathLike[str], setting: Any) -> IdlePolicy:
    """Persist ``setting`` (minutes or 'never') privately and atomically."""
    policy = IdlePolicy(parse_setting(setting))
    raw = json.dumps({"schema": 1, "unload_after_minutes": policy.setting}, sort_keys=True).encode("utf-8")
    path = Path(root) / POLICY_FILENAME
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return policy


def unload_after_s_or_never(root: str | os.PathLike[str], *, missing_means_never: bool = False) -> float | None:
    """Node-side reader: seconds, or ``None`` (never) when the file is unreadable (fail safe: keep serving).

    A node launched without an app that publishes the policy (gate harnesses, tools) has no file;
    with ``missing_means_never`` that is *never unload*, so only apps that publish a policy opt in.
    """
    try:
        if missing_means_never and not os.path.lexists(Path(root) / POLICY_FILENAME):
            return None
        return read_policy(root).unload_after_s
    except IdlePolicyError:
        return None
