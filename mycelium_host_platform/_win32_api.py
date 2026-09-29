# SPDX-License-Identifier: AGPL-3.0-or-later
"""The injectable Win32 API surface used by the win32 host backend.

The backend in :mod:`mycelium_host_platform._win32` never calls ``ctypes``
directly.  It calls one object implementing the methods documented on
:class:`Win32Api`.  On a real Windows host that object is
:class:`RealWin32Api` (``kernel32``/``advapi32`` through ``ctypes``); tests
inject a fake that models handles, SIDs, DACLs and sharing violations.

The real bindings are created lazily inside :func:`load_real_api`, so this
module imports on every host.  Off Windows the loader refuses with
``host_platform_unavailable``; nothing falls back to a POSIX primitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ._common import HostPlatformError


# ----------------------------------------------------------------- constants

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
GENERIC_ALL = 0x10000000
DELETE = 0x00010000
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
SYNCHRONIZE = 0x00100000
FILE_READ_DATA = 0x0001
FILE_WRITE_DATA = 0x0002
FILE_APPEND_DATA = 0x0004
FILE_WRITE_EA = 0x0010
FILE_DELETE_CHILD = 0x0040
FILE_READ_ATTRIBUTES = 0x0080
FILE_WRITE_ATTRIBUTES = 0x0100
FILE_ALL_ACCESS = 0x001F01FF

FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004

CREATE_NEW = 1
CREATE_ALWAYS = 2
OPEN_EXISTING = 3
OPEN_ALWAYS = 4

FILE_ATTRIBUTE_READONLY = 0x00000001
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_FLAG_WRITE_THROUGH = 0x80000000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

MOVEFILE_REPLACE_EXISTING = 0x00000001
MOVEFILE_WRITE_THROUGH = 0x00000008

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_FILE_EXISTS = 80
ERROR_ALREADY_EXISTS = 183

ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1

#: Rights that let a principal change an object's content or its protection.
WRITE_LIKE_RIGHTS = (
    GENERIC_WRITE
    | GENERIC_ALL
    | DELETE
    | WRITE_DAC
    | WRITE_OWNER
    | FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_EA
    | FILE_DELETE_CHILD
    | FILE_WRITE_ATTRIBUTES
)

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
TRUSTED_INSTALLER_SID = (
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
)


def owner_only_directory_sddl(user_sid: str) -> str:
    """Protected DACL granting only the user and SYSTEM, inherited by children."""

    return f"O:{user_sid}D:P(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)"


def owner_only_file_sddl(user_sid: str) -> str:
    """Protected DACL granting only the user and SYSTEM on one file."""

    return f"O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)"


# -------------------------------------------------------------------- values


class Win32Error(OSError):
    """One failed Win32 call; ``winerror`` is the ``GetLastError`` value."""

    def __init__(self, winerror: int, operation: str) -> None:
        self.winerror = int(winerror)
        self.operation = operation
        super().__init__(f"{operation} failed with Win32 error {int(winerror)}")


@dataclass(frozen=True)
class FileInformation:
    """``GetFileInformationByHandle`` projection (times in ns since 1601)."""

    attributes: int
    volume_serial: int
    file_index: int
    number_of_links: int
    size: int
    creation_ns: int
    last_write_ns: int


@dataclass(frozen=True)
class Ace:
    ace_type: int
    flags: int
    mask: int
    sid: str


@dataclass(frozen=True)
class SecurityDescriptor:
    """``GetSecurityInfo`` projection: owner, DACL protection and ACE list.

    ``aces`` is ``None`` for a NULL DACL (which grants everyone everything).
    """

    owner_sid: str | None
    dacl_protected: bool
    aces: tuple[Ace, ...] | None


class Win32Api(Protocol):
    """The Win32 operations the host backend relies on.

    Every method raises :class:`Win32Error` on failure; none returns an
    error sentinel.
    """

    def current_user_sid(self) -> str: ...

    def create_directory(self, path: str, sddl: str) -> None: ...

    def open(
        self,
        path: str,
        *,
        access: int,
        share: int,
        disposition: int,
        flags: int,
        sddl: str | None = None,
    ) -> int: ...

    def close(self, handle: int) -> None: ...

    def information(self, handle: int) -> FileInformation: ...

    def final_path(self, handle: int) -> str: ...

    def security(self, handle: int) -> SecurityDescriptor: ...

    def read(self, handle: int, size: int) -> bytes: ...

    def write(self, handle: int, data: bytes) -> int: ...

    def flush(self, handle: int) -> None: ...

    def move_replace(self, source: str, target: str) -> None: ...

    def delete(self, path: str) -> None: ...

    def handle_to_fd(self, handle: int) -> int: ...


# --------------------------------------------------------------- real binding


def load_real_api() -> "RealWin32Api":
    """Bind the real Win32 API; refuse (never degrade) off a Windows host."""

    import ctypes

    if not hasattr(ctypes, "WinDLL"):
        raise HostPlatformError("host_platform_unavailable", "win32_api_absent")
    try:
        return RealWin32Api()
    except (AttributeError, OSError) as exc:
        raise HostPlatformError("host_platform_unavailable", "win32_api_bind") from exc


def _long_path(path: str) -> str:
    """Extended-length form so MAX_PATH never silently truncates a path."""

    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _strip_long_path(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


class RealWin32Api:  # pragma: no cover - exercised only on a Windows device
    """``ctypes`` binding of :class:`Win32Api` (kernel32 + advapi32)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._kernel32 = kernel32
        self._advapi32 = advapi32
        HANDLE = wintypes.HANDLE
        DWORD = wintypes.DWORD
        BOOL = wintypes.BOOL
        LPCWSTR = wintypes.LPCWSTR
        LPVOID = wintypes.LPVOID

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", DWORD),
                ("lpSecurityDescriptor", LPVOID),
                ("bInheritHandle", BOOL),
            ]

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]

        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", DWORD),
                ("ftCreationTime", FILETIME),
                ("ftLastAccessTime", FILETIME),
                ("ftLastWriteTime", FILETIME),
                ("dwVolumeSerialNumber", DWORD),
                ("nFileSizeHigh", DWORD),
                ("nFileSizeLow", DWORD),
                ("nNumberOfLinks", DWORD),
                ("nFileIndexHigh", DWORD),
                ("nFileIndexLow", DWORD),
            ]

        class ACL_SIZE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("AceCount", DWORD),
                ("AclBytesInUse", DWORD),
                ("AclBytesFree", DWORD),
            ]

        class ACE_HEADER(ctypes.Structure):
            _fields_ = [
                ("AceType", ctypes.c_ubyte),
                ("AceFlags", ctypes.c_ubyte),
                ("AceSize", wintypes.WORD),
            ]

        class ACCESS_ALLOWED_ACE(ctypes.Structure):
            _fields_ = [
                ("Header", ACE_HEADER),
                ("Mask", DWORD),
                ("SidStart", DWORD),
            ]

        self.SECURITY_ATTRIBUTES = SECURITY_ATTRIBUTES
        self.FILETIME = FILETIME
        self.BY_HANDLE_FILE_INFORMATION = BY_HANDLE_FILE_INFORMATION
        self.ACL_SIZE_INFORMATION = ACL_SIZE_INFORMATION
        self.ACCESS_ALLOWED_ACE = ACCESS_ALLOWED_ACE

        def bind(library: Any, name: str, argtypes: list[Any], restype: Any) -> Any:
            function = getattr(library, name)
            function.argtypes = argtypes
            function.restype = restype
            return function

        self._CreateFileW = bind(
            kernel32,
            "CreateFileW",
            [LPCWSTR, DWORD, DWORD, LPVOID, DWORD, DWORD, HANDLE],
            HANDLE,
        )
        self._CreateDirectoryW = bind(kernel32, "CreateDirectoryW", [LPCWSTR, LPVOID], BOOL)
        self._CloseHandle = bind(kernel32, "CloseHandle", [HANDLE], BOOL)
        self._GetFileInformationByHandle = bind(
            kernel32,
            "GetFileInformationByHandle",
            [HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)],
            BOOL,
        )
        self._GetFinalPathNameByHandleW = bind(
            kernel32,
            "GetFinalPathNameByHandleW",
            [HANDLE, wintypes.LPWSTR, DWORD, DWORD],
            DWORD,
        )
        self._ReadFile = bind(
            kernel32,
            "ReadFile",
            [HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID],
            BOOL,
        )
        self._WriteFile = bind(
            kernel32,
            "WriteFile",
            [HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID],
            BOOL,
        )
        self._FlushFileBuffers = bind(kernel32, "FlushFileBuffers", [HANDLE], BOOL)
        self._MoveFileExW = bind(kernel32, "MoveFileExW", [LPCWSTR, LPCWSTR, DWORD], BOOL)
        self._DeleteFileW = bind(kernel32, "DeleteFileW", [LPCWSTR], BOOL)
        self._LocalFree = bind(kernel32, "LocalFree", [LPVOID], LPVOID)
        self._GetCurrentProcess = bind(kernel32, "GetCurrentProcess", [], HANDLE)
        self._ConvertSD = bind(
            advapi32,
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            [LPCWSTR, DWORD, ctypes.POINTER(LPVOID), ctypes.POINTER(wintypes.ULONG)],
            BOOL,
        )
        self._ConvertSidToStringSidW = bind(
            advapi32,
            "ConvertSidToStringSidW",
            [LPVOID, ctypes.POINTER(wintypes.LPWSTR)],
            BOOL,
        )
        self._GetSecurityInfo = bind(
            advapi32,
            "GetSecurityInfo",
            [
                HANDLE,
                DWORD,
                DWORD,
                ctypes.POINTER(LPVOID),
                ctypes.POINTER(LPVOID),
                ctypes.POINTER(LPVOID),
                ctypes.POINTER(LPVOID),
                ctypes.POINTER(LPVOID),
            ],
            DWORD,
        )
        self._GetSecurityDescriptorControl = bind(
            advapi32,
            "GetSecurityDescriptorControl",
            [LPVOID, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(DWORD)],
            BOOL,
        )
        self._GetAclInformation = bind(
            advapi32,
            "GetAclInformation",
            [LPVOID, LPVOID, DWORD, DWORD],
            BOOL,
        )
        self._GetAce = bind(
            advapi32, "GetAce", [LPVOID, DWORD, ctypes.POINTER(LPVOID)], BOOL
        )
        self._OpenProcessToken = bind(
            advapi32, "OpenProcessToken", [HANDLE, DWORD, ctypes.POINTER(HANDLE)], BOOL
        )
        self._GetTokenInformation = bind(
            advapi32,
            "GetTokenInformation",
            [HANDLE, DWORD, LPVOID, DWORD, ctypes.POINTER(DWORD)],
            BOOL,
        )
        self._invalid_handle = HANDLE(-1).value

    # ------------------------------------------------------------ helpers

    def _fail(self, operation: str) -> Win32Error:
        return Win32Error(self._ctypes.get_last_error(), operation)

    def _sid_string(self, sid_pointer: Any) -> str:
        ctypes = self._ctypes
        text = self._wintypes.LPWSTR()
        if not self._ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise self._fail("ConvertSidToStringSidW")
        try:
            return str(text.value)
        finally:
            self._LocalFree(ctypes.cast(text, self._wintypes.LPVOID))

    def _security_attributes(self, sddl: str | None) -> tuple[Any, Any]:
        ctypes = self._ctypes
        if sddl is None:
            return None, None
        descriptor = self._wintypes.LPVOID()
        if not self._ConvertSD(sddl, 1, ctypes.byref(descriptor), None):
            raise self._fail("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        attributes = self.SECURITY_ATTRIBUTES()
        attributes.nLength = ctypes.sizeof(attributes)
        attributes.lpSecurityDescriptor = descriptor
        attributes.bInheritHandle = False
        return attributes, descriptor

    @staticmethod
    def _filetime_ns(value: Any) -> int:
        ticks = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
        return ticks * 100

    # -------------------------------------------------------------- files

    def current_user_sid(self) -> str:
        ctypes = self._ctypes
        wintypes = self._wintypes
        token = wintypes.HANDLE()
        if not self._OpenProcessToken(self._GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise self._fail("OpenProcessToken")
        try:
            needed = wintypes.DWORD()
            self._GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if needed.value <= 0:
                raise self._fail("GetTokenInformation")
            buffer = ctypes.create_string_buffer(needed.value)
            if not self._GetTokenInformation(
                token, 1, buffer, needed.value, ctypes.byref(needed)
            ):
                raise self._fail("GetTokenInformation")
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
            return self._sid_string(sid_pointer)
        finally:
            self._CloseHandle(token)

    def create_directory(self, path: str, sddl: str) -> None:
        ctypes = self._ctypes
        attributes, descriptor = self._security_attributes(sddl)
        try:
            if not self._CreateDirectoryW(_long_path(path), ctypes.byref(attributes)):
                raise self._fail("CreateDirectoryW")
        finally:
            if descriptor is not None:
                self._LocalFree(descriptor)

    def open(
        self,
        path: str,
        *,
        access: int,
        share: int,
        disposition: int,
        flags: int,
        sddl: str | None = None,
    ) -> int:
        ctypes = self._ctypes
        attributes, descriptor = self._security_attributes(sddl)
        try:
            handle = self._CreateFileW(
                _long_path(path),
                access,
                share,
                ctypes.byref(attributes) if attributes is not None else None,
                disposition,
                flags,
                None,
            )
        finally:
            if descriptor is not None:
                self._LocalFree(descriptor)
        if handle is None or handle == self._invalid_handle:
            raise self._fail("CreateFileW")
        return int(handle)

    def close(self, handle: int) -> None:
        if not self._CloseHandle(handle):
            raise self._fail("CloseHandle")

    def information(self, handle: int) -> FileInformation:
        ctypes = self._ctypes
        value = self.BY_HANDLE_FILE_INFORMATION()
        if not self._GetFileInformationByHandle(handle, ctypes.byref(value)):
            raise self._fail("GetFileInformationByHandle")
        return FileInformation(
            attributes=int(value.dwFileAttributes),
            volume_serial=int(value.dwVolumeSerialNumber),
            file_index=(int(value.nFileIndexHigh) << 32) | int(value.nFileIndexLow),
            number_of_links=int(value.nNumberOfLinks),
            size=(int(value.nFileSizeHigh) << 32) | int(value.nFileSizeLow),
            creation_ns=self._filetime_ns(value.ftCreationTime),
            last_write_ns=self._filetime_ns(value.ftLastWriteTime),
        )

    def final_path(self, handle: int) -> str:
        ctypes = self._ctypes
        size = 1024
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            length = self._GetFinalPathNameByHandleW(handle, buffer, size, 0)
            if length == 0:
                raise self._fail("GetFinalPathNameByHandleW")
            if length < size:
                return _strip_long_path(buffer.value)
            if size >= 65536:
                raise Win32Error(ERROR_INVALID_HANDLE, "GetFinalPathNameByHandleW")
            size = length + 1

    def security(self, handle: int) -> SecurityDescriptor:
        ctypes = self._ctypes
        wintypes = self._wintypes
        owner = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        status = self._GetSecurityInfo(
            handle,
            1,  # SE_FILE_OBJECT
            0x00000001 | 0x00000004,  # OWNER | DACL
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            raise Win32Error(status, "GetSecurityInfo")
        try:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not self._GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise self._fail("GetSecurityDescriptorControl")
            owner_sid = self._sid_string(owner) if owner.value else None
            if not dacl.value:
                return SecurityDescriptor(owner_sid, bool(control.value & 0x1000), None)
            size = self.ACL_SIZE_INFORMATION()
            if not self._GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
                raise self._fail("GetAclInformation")
            aces: list[Ace] = []
            for index in range(int(size.AceCount)):
                pointer = wintypes.LPVOID()
                if not self._GetAce(dacl, index, ctypes.byref(pointer)):
                    raise self._fail("GetAce")
                ace = ctypes.cast(pointer, ctypes.POINTER(self.ACCESS_ALLOWED_ACE)).contents
                ace_type = int(ace.Header.AceType)
                if ace_type in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE):
                    sid_address = pointer.value + self.ACCESS_ALLOWED_ACE.SidStart.offset
                    sid = self._sid_string(wintypes.LPVOID(sid_address))
                else:
                    sid = ""
                aces.append(
                    Ace(
                        ace_type=ace_type,
                        flags=int(ace.Header.AceFlags),
                        mask=int(ace.Mask),
                        sid=sid,
                    )
                )
            return SecurityDescriptor(owner_sid, bool(control.value & 0x1000), tuple(aces))
        finally:
            self._LocalFree(descriptor)

    def read(self, handle: int, size: int) -> bytes:
        ctypes = self._ctypes
        buffer = ctypes.create_string_buffer(max(1, size))
        received = self._wintypes.DWORD()
        if not self._ReadFile(handle, buffer, size, ctypes.byref(received), None):
            raise self._fail("ReadFile")
        return buffer.raw[: received.value]

    def write(self, handle: int, data: bytes) -> int:
        ctypes = self._ctypes
        written = self._wintypes.DWORD()
        buffer = ctypes.create_string_buffer(data, len(data))
        if not self._WriteFile(handle, buffer, len(data), ctypes.byref(written), None):
            raise self._fail("WriteFile")
        return int(written.value)

    def flush(self, handle: int) -> None:
        if not self._FlushFileBuffers(handle):
            raise self._fail("FlushFileBuffers")

    def move_replace(self, source: str, target: str) -> None:
        if not self._MoveFileExW(
            _long_path(source),
            _long_path(target),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        ):
            raise self._fail("MoveFileExW")

    def delete(self, path: str) -> None:
        if not self._DeleteFileW(_long_path(path)):
            raise self._fail("DeleteFileW")

    def handle_to_fd(self, handle: int) -> int:
        import msvcrt

        import os

        return msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))


__all__ = [
    "Ace",
    "FileInformation",
    "RealWin32Api",
    "SecurityDescriptor",
    "Win32Api",
    "Win32Error",
    "load_real_api",
    "owner_only_directory_sddl",
    "owner_only_file_sddl",
]
