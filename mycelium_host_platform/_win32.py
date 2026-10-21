# SPDX-License-Identifier: AGPL-3.0-only
"""win32 backend: handle-bound private directories and atomic replacement.

Every operation runs over one injected :class:`~._win32_api.Win32Api`.  The
POSIX guarantees it substitutes are:

* ``O_NOFOLLOW``/``O_DIRECTORY`` walk -> ``CreateFileW`` with
  ``FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`` on every
  component, refusing any reparse point (symlink, junction, mount point);
* a retained directory descriptor -> a retained handle opened WITHOUT
  ``FILE_SHARE_DELETE``, so the directory cannot be renamed or removed while
  it is leased, plus ``(volume serial, file index)`` identity and a
  ``GetFinalPathNameByHandleW`` check against the leased path;
* mode ``0700``/``0600`` + owner uid -> a protected DACL granting only the
  signed-in user and SYSTEM, owned by the user, verified with
  ``GetSecurityInfo`` on the open handle;
* ``O_EXCL`` temp + ``fsync`` + ``rename`` + directory ``fsync`` ->
  ``CREATE_NEW`` with an owner-only security descriptor, ``FlushFileBuffers``,
  ``MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)`` with a bounded retry on
  sharing violations, then a directory-handle flush.

Every failure raises :class:`HostPlatformError`; nothing degrades to an
unverified success.
"""

from __future__ import annotations

import os
from pathlib import PurePath
import time
from contextlib import contextmanager
from typing import Any, BinaryIO, Iterable, Iterator
import uuid

from . import _win32_api as w
from ._common import ExecutableFingerprint, FileIdentity, HostPlatformError


REPLACE_ATTEMPTS = 12
REPLACE_BACKOFF_SECONDS = (0.005, 0.01, 0.02, 0.05, 0.1, 0.2)
_RETRYABLE_REPLACE_ERRORS = frozenset(
    {w.ERROR_SHARING_VIOLATION, w.ERROR_ACCESS_DENIED, w.ERROR_LOCK_VIOLATION}
)
_MISSING_ERRORS = frozenset({w.ERROR_FILE_NOT_FOUND, w.ERROR_PATH_NOT_FOUND})
_DIRECTORY_FLAGS = w.FILE_FLAG_BACKUP_SEMANTICS | w.FILE_FLAG_OPEN_REPARSE_POINT
_HELD_SHARE = w.FILE_SHARE_READ | w.FILE_SHARE_WRITE
_WALK_SHARE = w.FILE_SHARE_READ | w.FILE_SHARE_WRITE | w.FILE_SHARE_DELETE
_INHERIT_ONLY_ACE = 0x08
_READ_CHUNK = 64 * 1024


# ------------------------------------------------------------------ policy


def owner_only_violation(
    descriptor: w.SecurityDescriptor,
    user_sid: str,
    *,
    trusted_sids: Iterable[str] = (w.SYSTEM_SID,),
    require_protected: bool = True,
) -> str | None:
    """Return why ``descriptor`` is not owner-only, or ``None`` when it is.

    Owner-only means: owned by ``user_sid``; a present (non-NULL) DACL;
    protected from inheritance when ``require_protected``; every ACE an
    access-allowed ACE naming ``user_sid`` or one of ``trusted_sids``; and at
    least one ACE for the user.  Deny ACEs, object ACEs and unknown ACE types
    are refused rather than interpreted.
    """

    trusted = frozenset(trusted_sids) | {user_sid}
    if not isinstance(user_sid, str) or not user_sid.startswith("S-1-"):
        return "user_sid_invalid"
    if descriptor.owner_sid != user_sid:
        return "owner_not_user"
    if descriptor.aces is None:
        return "null_dacl"
    if require_protected and not descriptor.dacl_protected:
        return "dacl_not_protected"
    if not descriptor.aces:
        return "empty_dacl"
    user_granted = False
    for ace in descriptor.aces:
        if ace.ace_type != w.ACCESS_ALLOWED_ACE_TYPE:
            return "ace_type_refused"
        if ace.sid not in trusted:
            return "untrusted_principal"
        if ace.sid == user_sid and ace.mask:
            user_granted = True
    if not user_granted:
        return "user_not_granted"
    return None


def require_owner_only(api: Any, handle: int, user_sid: str) -> None:
    try:
        descriptor = api.security(handle)
    except w.Win32Error as exc:
        raise HostPlatformError("host_permissions_invalid", "security_unreadable") from exc
    reason = owner_only_violation(descriptor, user_sid)
    if reason is not None:
        raise HostPlatformError("host_permissions_invalid", reason)


def _writable_by_untrusted(descriptor: w.SecurityDescriptor, user_sid: str) -> bool:
    trusted = {
        user_sid,
        w.SYSTEM_SID,
        w.ADMINISTRATORS_SID,
        w.TRUSTED_INSTALLER_SID,
    }
    if descriptor.aces is None:
        return True
    for ace in descriptor.aces:
        if ace.flags & _INHERIT_ONLY_ACE:
            continue
        if ace.ace_type == w.ACCESS_DENIED_ACE_TYPE:
            continue
        if ace.ace_type != w.ACCESS_ALLOWED_ACE_TYPE:
            return True
        if ace.sid not in trusted and ace.mask & w.WRITE_LIKE_RIGHTS:
            return True
    return False


# ----------------------------------------------------------------- helpers


def _same_path(left: str, right: str) -> bool:
    return os.path.normpath(left).casefold() == os.path.normpath(right).casefold()


def _component(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or any(separator in name for separator in ("/", "\\", ":", "\x00"))
        or name != name.rstrip(" .")
    ):
        raise HostPlatformError("host_path_invalid", "component")
    return name


def _absolute(path: str | os.PathLike[str]) -> str:
    text = os.fspath(path)
    if not isinstance(text, str) or not text or "\x00" in text:
        raise HostPlatformError("host_path_invalid", "path")
    if not os.path.isabs(text):
        raise HostPlatformError("host_path_invalid", "relative")
    return os.path.normpath(text)


def _close_quietly(api: Any, handle: int | None) -> None:
    if handle is None:
        return
    try:
        api.close(handle)
    except w.Win32Error:
        pass


def _information(api: Any, handle: int) -> w.FileInformation:
    try:
        return api.information(handle)
    except w.Win32Error as exc:
        raise HostPlatformError("host_io_failed", "information") from exc


def _require_directory(information: w.FileInformation) -> None:
    if information.attributes & w.FILE_ATTRIBUTE_REPARSE_POINT:
        raise HostPlatformError("host_path_invalid", "reparse_point")
    if not information.attributes & w.FILE_ATTRIBUTE_DIRECTORY:
        raise HostPlatformError("host_path_invalid", "not_directory")


def _require_plain_file(information: w.FileInformation) -> None:
    if information.attributes & w.FILE_ATTRIBUTE_REPARSE_POINT:
        raise HostPlatformError("host_path_invalid", "reparse_point")
    if information.attributes & w.FILE_ATTRIBUTE_DIRECTORY:
        raise HostPlatformError("host_file_invalid", "directory")


def _require_final_path(api: Any, handle: int, expected: str) -> None:
    try:
        observed = api.final_path(handle)
    except w.Win32Error as exc:
        raise HostPlatformError("host_path_invalid", "final_path") from exc
    if not _same_path(observed, expected):
        raise HostPlatformError("host_path_invalid", "final_path_mismatch")


def _open_directory(api: Any, path: str, *, access: int, share: int) -> int:
    return api.open(
        path,
        access=access,
        share=share,
        disposition=w.OPEN_EXISTING,
        flags=_DIRECTORY_FLAGS,
    )


def _identity(information: w.FileInformation) -> FileIdentity:
    return FileIdentity(information.volume_serial, information.file_index)


def _user_sid(api: Any) -> str:
    try:
        sid = api.current_user_sid()
    except w.Win32Error as exc:
        raise HostPlatformError("host_platform_unavailable", "token_user") from exc
    if not isinstance(sid, str) or not sid.startswith("S-1-"):
        raise HostPlatformError("host_platform_unavailable", "token_user")
    return sid


# -------------------------------------------------------- private directory


class Win32PrivateDirectory:
    """One leased owner-only directory held open by handle."""

    def __init__(
        self,
        api: Any,
        user_sid: str,
        path: str,
        *,
        parent_handle: int,
        handle: int | None,
    ) -> None:
        self._api = api
        self._user_sid = user_sid
        self.path = path
        self._parent_handle: int | None = parent_handle
        self._handle = handle
        self._closed = False
        self._identity: FileIdentity | None = None
        self._parent_identity = _identity(_information(api, parent_handle))
        if handle is not None:
            self._identity = _identity(_information(api, handle))

    # ------------------------------------------------------------ state

    @property
    def exists(self) -> bool:
        return self._handle is not None

    @property
    def identity(self) -> FileIdentity | None:
        return self._identity

    def _require_handle(self) -> int:
        if self._closed or self._handle is None:
            raise HostPlatformError("host_path_invalid", "directory_unavailable")
        return self._handle

    def revalidate(self) -> None:
        """The leased handle still names this owner-only directory at ``path``."""

        handle = self._require_handle()
        information = _information(self._api, handle)
        _require_directory(information)
        if _identity(information) != self._identity:
            raise HostPlatformError("host_path_invalid", "identity_changed")
        assert self._parent_handle is not None
        if _identity(_information(self._api, self._parent_handle)) != self._parent_identity:
            raise HostPlatformError("host_path_invalid", "parent_identity_changed")
        _require_final_path(self._api, handle, self.path)
        require_owner_only(self._api, handle, self._user_sid)

    def _child(self, name: str) -> str:
        return os.path.join(self.path, _component(name))

    # ---------------------------------------------------------- children

    def subdirectory(self, name: str) -> "Win32PrivateDirectory":
        self.revalidate()
        child = self._child(name)
        try:
            self._api.create_directory(
                child, w.owner_only_directory_sddl(self._user_sid)
            )
        except w.Win32Error as exc:
            if exc.winerror != w.ERROR_ALREADY_EXISTS:
                raise HostPlatformError("host_io_failed", "create_directory") from exc
        return open_private_directory(self._api, child, create=False)

    def read_file(self, name: str, *, max_bytes: int) -> bytes:
        """Read one owner-only, single-link, non-reparse regular file."""

        self.revalidate()
        path = self._child(name)
        try:
            handle = self._api.open(
                path,
                access=w.GENERIC_READ,
                share=w.FILE_SHARE_READ,
                disposition=w.OPEN_EXISTING,
                flags=w.FILE_FLAG_OPEN_REPARSE_POINT,
            )
        except w.Win32Error as exc:
            if exc.winerror in _MISSING_ERRORS:
                raise HostPlatformError("host_file_missing") from exc
            if exc.winerror == w.ERROR_SHARING_VIOLATION:
                # Another handle holds it without sharing: a writer is mid-create.
                raise HostPlatformError("host_io_failed", "sharing_violation") from exc
            raise HostPlatformError("host_io_failed", "open") from exc
        try:
            before = _information(self._api, handle)
            _require_plain_file(before)
            if before.number_of_links != 1:
                raise HostPlatformError("host_file_invalid", "link_count")
            if before.size > max_bytes:
                raise HostPlatformError("host_file_invalid", "size")
            _require_final_path(self._api, handle, path)
            require_owner_only(self._api, handle, self._user_sid)
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                try:
                    chunk = self._api.read(handle, min(_READ_CHUNK, remaining))
                except w.Win32Error as exc:
                    raise HostPlatformError("host_io_failed", "read") from exc
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = _information(self._api, handle)
            if (
                len(raw) != before.size
                or len(raw) > max_bytes
                or after != before
            ):
                raise HostPlatformError("host_file_invalid", "changed_during_read")
            return raw
        finally:
            _close_quietly(self._api, handle)

    def require_file(self, name: str) -> None:
        """``name`` is an owner-only, single-link, non-reparse regular file.

        The checks of :meth:`read_file` without reading the bytes, for a file
        another component opens by path afterwards (an SQLite database).
        """

        self.revalidate()
        path = self._child(name)
        try:
            handle = self._api.open(
                path,
                access=w.GENERIC_READ,
                share=w.FILE_SHARE_READ | w.FILE_SHARE_WRITE,
                disposition=w.OPEN_EXISTING,
                flags=w.FILE_FLAG_OPEN_REPARSE_POINT,
            )
        except w.Win32Error as exc:
            if exc.winerror in _MISSING_ERRORS:
                raise HostPlatformError("host_file_missing") from exc
            raise HostPlatformError("host_io_failed", "open") from exc
        try:
            information = _information(self._api, handle)
            _require_plain_file(information)
            if information.number_of_links != 1:
                raise HostPlatformError("host_file_invalid", "link_count")
            _require_final_path(self._api, handle, path)
            require_owner_only(self._api, handle, self._user_sid)
        finally:
            _close_quietly(self._api, handle)

    def _write_new(self, path: str, data: bytes) -> None:
        """CREATE_NEW an owner-only file and durably write ``data`` to it."""

        try:
            handle = self._api.open(
                path,
                access=w.GENERIC_READ | w.GENERIC_WRITE | w.DELETE,
                share=0,
                disposition=w.CREATE_NEW,
                flags=w.FILE_FLAG_OPEN_REPARSE_POINT | w.FILE_FLAG_WRITE_THROUGH,
                sddl=w.owner_only_file_sddl(self._user_sid),
            )
        except w.Win32Error as exc:
            if exc.winerror in {w.ERROR_FILE_EXISTS, w.ERROR_ALREADY_EXISTS}:
                raise HostPlatformError("host_file_exists") from exc
            raise HostPlatformError("host_io_failed", "create") from exc
        try:
            information = _information(self._api, handle)
            _require_plain_file(information)
            if information.number_of_links != 1:
                raise HostPlatformError("host_file_invalid", "link_count")
            require_owner_only(self._api, handle, self._user_sid)
            written = 0
            while written < len(data):
                try:
                    count = self._api.write(handle, data[written:])
                except w.Win32Error as exc:
                    raise HostPlatformError("host_io_failed", "write") from exc
                if count <= 0:
                    raise HostPlatformError("host_io_failed", "short_write")
                written += count
            try:
                self._api.flush(handle)
            except w.Win32Error as exc:
                raise HostPlatformError("host_io_failed", "flush") from exc
        finally:
            _close_quietly(self._api, handle)

    def _delete_quietly(self, path: str) -> None:
        try:
            self._api.delete(path)
        except w.Win32Error:
            pass

    @contextmanager
    def exclusive_lock(self, name: str, *, blocking: bool = True) -> Iterator[None]:
        """Hold an exclusive ``LockFileEx`` lock on the owner-only file ``name``."""

        self.revalidate()
        path = self._child(name)
        handle = None
        for disposition in (w.CREATE_NEW, w.OPEN_EXISTING):
            try:
                handle = self._api.open(
                    path,
                    access=w.GENERIC_READ | w.GENERIC_WRITE,
                    share=w.FILE_SHARE_READ | w.FILE_SHARE_WRITE,
                    disposition=disposition,
                    flags=w.FILE_FLAG_OPEN_REPARSE_POINT,
                    sddl=(
                        w.owner_only_file_sddl(self._user_sid)
                        if disposition == w.CREATE_NEW
                        else None
                    ),
                )
                break
            except w.Win32Error as exc:
                if disposition == w.CREATE_NEW and exc.winerror in {
                    w.ERROR_FILE_EXISTS,
                    w.ERROR_ALREADY_EXISTS,
                }:
                    continue
                raise HostPlatformError("host_io_failed", "lock_open") from exc
        assert handle is not None
        try:
            information = _information(self._api, handle)
            _require_plain_file(information)
            _require_final_path(self._api, handle, path)
            require_owner_only(self._api, handle, self._user_sid)
            try:
                self._api.lock_file(handle, blocking=blocking)
            except w.Win32Error as exc:
                if exc.winerror == w.ERROR_LOCK_VIOLATION:
                    raise HostPlatformError("host_lock_busy") from exc
                raise HostPlatformError("host_io_failed", "lock") from exc
            try:
                yield
            finally:
                try:
                    self._api.unlock_file(handle)
                except w.Win32Error:
                    pass
        finally:
            _close_quietly(self._api, handle)

    def create_file_exclusive(self, name: str, data: bytes) -> None:
        """Create ``name`` once; ``host_file_exists`` when it is already there."""

        self.revalidate()
        path = self._child(name)
        try:
            self._write_new(path, data)
        except HostPlatformError as exc:
            if exc.code != "host_file_exists":
                self._delete_quietly(path)
            raise
        self.flush()

    def atomic_replace(self, name: str, data: bytes) -> None:
        """Replace ``name`` with ``data`` atomically and durably."""

        self.revalidate()
        target = self._child(name)
        temporary = self._child(f".{_component(name)}.{uuid.uuid4().hex}.tmp")
        try:
            self._write_new(temporary, data)
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    self._api.move_replace(temporary, target)
                    break
                except w.Win32Error as exc:
                    if exc.winerror not in _RETRYABLE_REPLACE_ERRORS:
                        raise HostPlatformError("host_io_failed", "replace") from exc
                    if attempt == REPLACE_ATTEMPTS - 1:
                        raise HostPlatformError("host_replace_contended") from exc
                    time.sleep(
                        REPLACE_BACKOFF_SECONDS[
                            min(attempt, len(REPLACE_BACKOFF_SECONDS) - 1)
                        ]
                    )
            self.flush()
        finally:
            self._delete_quietly(temporary)
        self.revalidate()

    def flush(self) -> None:
        """Flush directory metadata through a handle bound to the leased identity."""

        self._require_handle()
        try:
            handle = _open_directory(
                self._api,
                self.path,
                access=w.GENERIC_READ | w.GENERIC_WRITE,
                share=_WALK_SHARE,
            )
        except w.Win32Error as exc:
            raise HostPlatformError("host_io_failed", "flush_open") from exc
        try:
            information = _information(self._api, handle)
            _require_directory(information)
            if _identity(information) != self._identity:
                raise HostPlatformError("host_path_invalid", "identity_changed")
            try:
                self._api.flush(handle)
            except w.Win32Error as exc:
                raise HostPlatformError("host_io_failed", "flush") from exc
        finally:
            _close_quietly(self._api, handle)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[w.Win32Error] = []
        for handle in (self._handle, self._parent_handle):
            if handle is None:
                continue
            try:
                self._api.close(handle)
            except w.Win32Error as exc:
                failures.append(exc)
        self._handle = None
        self._parent_handle = None
        if failures:
            raise HostPlatformError("host_io_failed", "close") from failures[0]

    def __enter__(self) -> "Win32PrivateDirectory":
        if self._closed:
            raise HostPlatformError("host_path_invalid", "directory_unavailable")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_private_directory(
    api: Any,
    path: str | os.PathLike[str],
    *,
    create: bool,
) -> Win32PrivateDirectory:
    """Walk ``path`` refusing reparse points; lease the final owner-only directory."""

    target = _absolute(path)
    parts = PurePath(target).parts
    if len(parts) < 2:
        raise HostPlatformError("host_path_invalid", "root")
    user_sid = _user_sid(api)
    directory_sddl = w.owner_only_directory_sddl(user_sid)
    parent: int | None = None
    current = parts[0]
    try:
        for part in parts[1:-1]:
            current = os.path.join(current, part)
            _close_quietly(
                api,
                _open_walk_component(api, current, create=create, sddl=directory_sddl),
            )
        # Retain the final directory's parent.  Below the volume root it is
        # held without FILE_SHARE_DELETE so it cannot be renamed underneath.
        at_root = current == parts[0]
        try:
            parent = _open_directory(
                api,
                current,
                access=w.FILE_READ_ATTRIBUTES,
                share=_WALK_SHARE if at_root else _HELD_SHARE,
            )
        except w.Win32Error as exc:
            raise HostPlatformError("host_path_invalid", "parent") from exc
        _require_directory(_information(api, parent))
        if not at_root:
            _require_final_path(api, parent, current)
        handle: int | None
        try:
            handle = _open_directory(
                api,
                target,
                access=w.GENERIC_READ,
                share=_HELD_SHARE,
            )
        except w.Win32Error as exc:
            if exc.winerror not in _MISSING_ERRORS:
                raise HostPlatformError("host_path_invalid", "open") from exc
            if not create:
                lease = Win32PrivateDirectory(
                    api, user_sid, target, parent_handle=parent, handle=None
                )
                parent = None
                return lease
            try:
                api.create_directory(target, directory_sddl)
            except w.Win32Error as create_exc:
                if create_exc.winerror != w.ERROR_ALREADY_EXISTS:
                    raise HostPlatformError("host_io_failed", "create_directory") from create_exc
            try:
                handle = _open_directory(
                    api, target, access=w.GENERIC_READ, share=_HELD_SHARE
                )
            except w.Win32Error as open_exc:
                raise HostPlatformError("host_path_invalid", "open") from open_exc
        try:
            _require_directory(_information(api, handle))
            lease = Win32PrivateDirectory(
                api, user_sid, target, parent_handle=parent, handle=handle
            )
        except BaseException:
            _close_quietly(api, handle)
            raise
        parent = None
        try:
            lease.revalidate()
        except BaseException:
            lease.close()
            raise
        return lease
    finally:
        _close_quietly(api, parent)


def _open_walk_component(api: Any, path: str, *, create: bool, sddl: str) -> int:
    try:
        handle = _open_directory(
            api, path, access=w.FILE_READ_ATTRIBUTES, share=_WALK_SHARE
        )
    except w.Win32Error as exc:
        if exc.winerror not in _MISSING_ERRORS or not create:
            raise HostPlatformError("host_path_invalid", "ancestor") from exc
        try:
            api.create_directory(path, sddl)
        except w.Win32Error as create_exc:
            if create_exc.winerror != w.ERROR_ALREADY_EXISTS:
                raise HostPlatformError("host_io_failed", "create_directory") from create_exc
        try:
            handle = _open_directory(
                api, path, access=w.FILE_READ_ATTRIBUTES, share=_WALK_SHARE
            )
        except w.Win32Error as open_exc:
            raise HostPlatformError("host_path_invalid", "ancestor") from open_exc
    try:
        _require_directory(_information(api, handle))
    except BaseException:
        _close_quietly(api, handle)
        raise
    return handle


# ------------------------------------------------------------ open beneath


def open_beneath(
    api: Any,
    root: str | os.PathLike[str],
    relative_parts: tuple[str, ...],
) -> BinaryIO:
    """Open ``root/relative_parts`` read-only, refusing every reparse point."""

    if not relative_parts:
        raise HostPlatformError("host_path_invalid", "empty")
    base = _absolute(root)
    held: list[int] = []
    file_handle: int | None = None
    try:
        try:
            root_handle = _open_directory(
                api, base, access=w.FILE_READ_ATTRIBUTES, share=_HELD_SHARE
            )
        except w.Win32Error as exc:
            raise HostPlatformError("host_path_invalid", "root") from exc
        held.append(root_handle)
        _require_directory(_information(api, root_handle))
        _require_final_path(api, root_handle, base)
        current = base
        for part in relative_parts[:-1]:
            current = os.path.join(current, _component(part))
            try:
                child = _open_directory(
                    api, current, access=w.FILE_READ_ATTRIBUTES, share=_HELD_SHARE
                )
            except w.Win32Error as exc:
                raise HostPlatformError("host_path_invalid", "component") from exc
            held.append(child)
            _require_directory(_information(api, child))
            _require_final_path(api, child, current)
        path = os.path.join(current, _component(relative_parts[-1]))
        try:
            file_handle = api.open(
                path,
                access=w.GENERIC_READ,
                share=w.FILE_SHARE_READ,
                disposition=w.OPEN_EXISTING,
                flags=w.FILE_FLAG_OPEN_REPARSE_POINT,
            )
        except w.Win32Error as exc:
            if exc.winerror in _MISSING_ERRORS:
                raise HostPlatformError("host_file_missing") from exc
            raise HostPlatformError("host_io_failed", "open") from exc
        _require_plain_file(_information(api, file_handle))
        _require_final_path(api, file_handle, path)
        try:
            descriptor = api.handle_to_fd(file_handle)
        except (OSError, ValueError) as exc:
            raise HostPlatformError("host_io_failed", "descriptor") from exc
        file_handle = None
        return os.fdopen(descriptor, "rb")
    finally:
        _close_quietly(api, file_handle)
        for handle in reversed(held):
            _close_quietly(api, handle)


# --------------------------------------------------------- user-owned file


def read_owner_file(
    api: Any,
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
) -> bytes:
    """Read one single-link plain file readable only by user, SYSTEM, Administrators.

    The Windows analogue of a POSIX ``0600``-style user file outside a
    private directory (an invite bundle in the user's profile): the DACL may
    be inherited, but it must be owned by the token user and grant nobody
    else.
    """

    target = _absolute(path)
    user_sid = _user_sid(api)
    try:
        handle = api.open(
            target,
            access=w.GENERIC_READ,
            share=w.FILE_SHARE_READ,
            disposition=w.OPEN_EXISTING,
            flags=w.FILE_FLAG_OPEN_REPARSE_POINT,
        )
    except w.Win32Error as exc:
        if exc.winerror in _MISSING_ERRORS:
            raise HostPlatformError("host_file_missing") from exc
        raise HostPlatformError("host_io_failed", "open") from exc
    try:
        before = _information(api, handle)
        _require_plain_file(before)
        if before.number_of_links != 1:
            raise HostPlatformError("host_file_invalid", "link_count")
        if before.size <= 0 or before.size > max_bytes:
            raise HostPlatformError("host_file_invalid", "size")
        _require_final_path(api, handle, target)
        try:
            descriptor = api.security(handle)
        except w.Win32Error as exc:
            raise HostPlatformError("host_permissions_invalid", "security_unreadable") from exc
        reason = owner_only_violation(
            descriptor,
            user_sid,
            trusted_sids=(w.SYSTEM_SID, w.ADMINISTRATORS_SID),
            require_protected=False,
        )
        if reason is not None:
            raise HostPlatformError("host_permissions_invalid", reason)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            try:
                chunk = api.read(handle, min(_READ_CHUNK, remaining))
            except w.Win32Error as exc:
                raise HostPlatformError("host_io_failed", "read") from exc
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != before.size or _information(api, handle) != before:
            raise HostPlatformError("host_file_invalid", "changed_during_read")
        return raw
    finally:
        _close_quietly(api, handle)


# ------------------------------------------------------ executable identity


def executable_fingerprint(
    api: Any,
    path: str | os.PathLike[str],
    *,
    require_private_owner: bool,
) -> ExecutableFingerprint:
    """Handle-bound fingerprint of one executable; refuses reparse points."""

    target = _absolute(path)
    try:
        handle = api.open(
            target,
            access=w.GENERIC_READ,
            share=w.FILE_SHARE_READ,
            disposition=w.OPEN_EXISTING,
            flags=w.FILE_FLAG_OPEN_REPARSE_POINT,
        )
    except w.Win32Error as exc:
        if exc.winerror in _MISSING_ERRORS:
            raise HostPlatformError("host_file_missing") from exc
        raise HostPlatformError("host_io_failed", "open") from exc
    try:
        information = _information(api, handle)
        _require_plain_file(information)
        _require_final_path(api, handle, target)
        if require_private_owner:
            user_sid = _user_sid(api)
            try:
                descriptor = api.security(handle)
            except w.Win32Error as exc:
                raise HostPlatformError("host_permissions_invalid", "security_unreadable") from exc
            if descriptor.owner_sid != user_sid or _writable_by_untrusted(
                descriptor, user_sid
            ):
                raise HostPlatformError("host_permissions_invalid", "executable_acl")
        return ExecutableFingerprint(
            path=target,
            volume=information.volume_serial,
            index=information.file_index,
            size=information.size,
            last_write_ns=information.last_write_ns,
            creation_ns=information.creation_ns,
        )
    finally:
        _close_quietly(api, handle)


__all__ = [
    "REPLACE_ATTEMPTS",
    "Win32PrivateDirectory",
    "executable_fingerprint",
    "open_beneath",
    "open_private_directory",
    "owner_only_violation",
    "read_owner_file",
    "require_owner_only",
]
