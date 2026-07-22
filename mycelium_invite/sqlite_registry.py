"""Durable atomic invite replay protection backed by SQLite."""
from __future__ import annotations

import math
import os
from pathlib import Path
import sqlite3
import stat

from .token import InviteError


class SqliteInviteRegistry:
    """Persist consumed nonces across restarts and concurrent join attempts."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        self._prepare_path()
        self._initialize()

    def _prepare_path(self) -> None:
        parent = self.database.parent
        try:
            missing: list[Path] = []
            nearest = parent
            while not nearest.exists() and not nearest.is_symlink():
                missing.append(nearest)
                if nearest == nearest.parent:
                    break
                nearest = nearest.parent

            for ancestor in (nearest, *nearest.parents):
                metadata = ancestor.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise InviteError("invite_registry_path_invalid")
            if not stat.S_ISDIR(nearest.lstat().st_mode):
                raise InviteError("invite_registry_path_invalid")

            for directory in reversed(missing):
                directory.mkdir(mode=0o700)
                directory.chmod(0o700)

            metadata = parent.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise InviteError("invite_registry_path_invalid")
            if (
                stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.getuid()
            ):
                raise InviteError("invite_registry_permissions_invalid")

            if self.database.exists() or self.database.is_symlink():
                metadata = self.database.lstat()
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise InviteError("invite_registry_path_invalid")
                if (
                    stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_uid != os.getuid()
                    or metadata.st_nlink != 1
                ):
                    raise InviteError("invite_registry_permissions_invalid")
            else:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(self.database, flags, 0o600)
                try:
                    metadata = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_uid != os.getuid()
                        or metadata.st_nlink != 1
                    ):
                        raise InviteError("invite_registry_permissions_invalid")
                finally:
                    os.close(descriptor)
        except InviteError:
            raise
        except OSError as exc:
            raise InviteError("invite_registry_path_invalid") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=30.0,
            isolation_level=None,
        )
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consumed_invites (
                    nonce TEXT PRIMARY KEY NOT NULL,
                    consumed_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                ) WITHOUT ROWID
                """
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise InviteError("invite_registry_unavailable") from exc
        finally:
            connection.close()

    @staticmethod
    def _validate(nonce: str, now: float, expires_at: float) -> None:
        if not isinstance(nonce, str) or not nonce:
            raise InviteError("invite_field_invalid")
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
        ):
            raise InviteError("invite_time_invalid")
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
        ):
            raise InviteError("invite_time_invalid")
        if float(now) > float(expires_at):
            raise InviteError("invite_expired")

    def consume(self, nonce: str, now: float, expires_at: float) -> None:
        """Atomically consume one live nonce or reject an existing row as replay."""

        self._validate(nonce, now, expires_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO consumed_invites (nonce, consumed_at, expires_at) VALUES (?, ?, ?)",
                (nonce, float(now), float(expires_at)),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise InviteError("invite_replayed") from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise InviteError("invite_registry_unavailable") from exc
        finally:
            connection.close()

    def prune_expired(self, *, now: float) -> int:
        """Delete rows whose invitation validity ended strictly before ``now``."""

        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
        ):
            raise InviteError("invite_time_invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM consumed_invites WHERE expires_at < ?",
                (float(now),),
            )
            connection.commit()
            return int(cursor.rowcount)
        except sqlite3.Error as exc:
            connection.rollback()
            raise InviteError("invite_registry_unavailable") from exc
        finally:
            connection.close()
