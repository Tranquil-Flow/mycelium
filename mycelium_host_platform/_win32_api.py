# SPDX-License-Identifier: AGPL-3.0-only
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

LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

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
ERROR_MORE_DATA = 234
ERROR_INVALID_PARAMETER = 87

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

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
        # On Windows, OSError.__init__ resets the C-level winerror slot, so the
        # attributes must be assigned after it (else winerror is None).
        super().__init__(f"{operation} failed with Win32 error {int(winerror)}")
        self.winerror = int(winerror)
        self.operation = operation


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

    def lock_file(self, handle: int, *, blocking: bool) -> None: ...

    def unlock_file(self, handle: int) -> None: ...

    def move_replace(self, source: str, target: str) -> None: ...

    def delete(self, path: str) -> None: ...

    def handle_to_fd(self, handle: int) -> int: ...

    # Processes and job objects.

    def spawn_suspended(self, argv: list[str], *, creationflags: int, **popen: Any) -> Any: ...

    def open_process(self, pid: int) -> int: ...

    def current_process(self) -> int: ...

    def process_image_name(self, process: int) -> str: ...

    def process_creation_time(self, process: int) -> int: ...

    def resume_process(self, pid: int) -> int: ...

    def terminate_process(self, process: int, exit_code: int) -> None: ...

    def create_job(self) -> int: ...

    def set_job_limits(self, job: int, limit_flags: int) -> None: ...

    def job_limit_flags(self, job: int) -> int: ...

    def assign_process(self, job: int, process: int) -> None: ...

    def is_process_in_job(self, process: int, job: int) -> bool: ...

    def job_active_processes(self, job: int) -> int: ...

    def job_process_ids(self, job: int) -> list[int]: ...

    def process_working_set_bytes_of(self, process: int) -> int: ...

    def terminate_job(self, job: int, exit_code: int) -> None: ...

    # Memory and named events.

    def process_working_set_bytes(self) -> int: ...

    def available_physical_bytes(self) -> int: ...

    def total_physical_bytes(self) -> int: ...

    def open_event(self, name: str) -> int: ...

    def wait_event(self, event: int, timeout_ms: int) -> bool: ...

    def create_event(self, name: str) -> tuple[int, bool]: ...

    def set_event(self, event: int) -> None: ...


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

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", DWORD),
                ("OffsetHigh", DWORD),
                ("hEvent", HANDLE),
            ]

        self.OVERLAPPED = OVERLAPPED
        self._LockFileEx = bind(
            kernel32,
            "LockFileEx",
            [HANDLE, DWORD, DWORD, DWORD, DWORD, ctypes.POINTER(OVERLAPPED)],
            BOOL,
        )
        self._UnlockFileEx = bind(
            kernel32,
            "UnlockFileEx",
            [HANDLE, DWORD, DWORD, DWORD, ctypes.POINTER(OVERLAPPED)],
            BOOL,
        )
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

    def lock_file(self, handle: int, *, blocking: bool) -> None:
        """Exclusive byte-range lock on byte 0 (``LockFileEx``)."""

        flags = LOCKFILE_EXCLUSIVE_LOCK | (0 if blocking else LOCKFILE_FAIL_IMMEDIATELY)
        overlapped = self.OVERLAPPED()
        if not self._LockFileEx(handle, flags, 0, 1, 0, self._ctypes.byref(overlapped)):
            raise self._fail("LockFileEx")

    def unlock_file(self, handle: int) -> None:
        overlapped = self.OVERLAPPED()
        if not self._UnlockFileEx(handle, 0, 1, 0, self._ctypes.byref(overlapped)):
            raise self._fail("UnlockFileEx")

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

    # ---------------------------------------------------- process and job

    def _bind_process_api(self) -> None:
        if getattr(self, "_process_api_bound", False):
            return
        ctypes = self._ctypes
        wintypes = self._wintypes
        kernel32 = self._kernel32
        HANDLE, DWORD, BOOL = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", DWORD),
                ("SchedulingClass", DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", DWORD),
                ("TotalProcesses", DWORD),
                ("ActiveProcesses", DWORD),
                ("TotalTerminatedProcesses", DWORD),
            ]

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", DWORD),
                ("cntUsage", DWORD),
                ("th32ThreadID", DWORD),
                ("th32OwnerProcessID", DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", DWORD),
            ]

        self.JOBOBJECT_EXTENDED_LIMIT_INFORMATION = JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        self.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
        self.THREADENTRY32 = THREADENTRY32

        def bind(name: str, argtypes: list[Any], restype: Any) -> Any:
            function = getattr(kernel32, name)
            function.argtypes = argtypes
            function.restype = restype
            return function

        self._OpenProcess = bind("OpenProcess", [DWORD, BOOL, DWORD], HANDLE)
        self._QueryFullProcessImageNameW = bind(
            "QueryFullProcessImageNameW",
            [HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)],
            BOOL,
        )
        self._GetProcessTimes = bind(
            "GetProcessTimes",
            [HANDLE] + [ctypes.POINTER(self.FILETIME)] * 4,
            BOOL,
        )
        self._CreateToolhelp32Snapshot = bind("CreateToolhelp32Snapshot", [DWORD, DWORD], HANDLE)
        self._Thread32First = bind("Thread32First", [HANDLE, ctypes.POINTER(THREADENTRY32)], BOOL)
        self._Thread32Next = bind("Thread32Next", [HANDLE, ctypes.POINTER(THREADENTRY32)], BOOL)
        self._OpenThread = bind("OpenThread", [DWORD, BOOL, DWORD], HANDLE)
        self._ResumeThread = bind("ResumeThread", [HANDLE], DWORD)
        self._TerminateProcess = bind("TerminateProcess", [HANDLE, wintypes.UINT], BOOL)
        self._CreateJobObjectW = bind("CreateJobObjectW", [wintypes.LPVOID, wintypes.LPCWSTR], HANDLE)
        self._SetInformationJobObject = bind(
            "SetInformationJobObject", [HANDLE, ctypes.c_int, wintypes.LPVOID, DWORD], BOOL
        )
        self._QueryInformationJobObject = bind(
            "QueryInformationJobObject",
            [HANDLE, ctypes.c_int, wintypes.LPVOID, DWORD, ctypes.POINTER(DWORD)],
            BOOL,
        )
        self._AssignProcessToJobObject = bind("AssignProcessToJobObject", [HANDLE, HANDLE], BOOL)
        self._IsProcessInJob = bind("IsProcessInJob", [HANDLE, HANDLE, ctypes.POINTER(BOOL)], BOOL)
        self._TerminateJobObject = bind("TerminateJobObject", [HANDLE, wintypes.UINT], BOOL)
        self._process_api_bound = True

    def spawn_suspended(self, argv: list[str], *, creationflags: int, **popen: Any) -> Any:
        import subprocess

        return subprocess.Popen(argv, creationflags=creationflags, **popen)

    def open_process(self, pid: int) -> int:
        self._bind_process_api()
        # QUERY_LIMITED_INFORMATION | TERMINATE | SET_QUOTA | SYNCHRONIZE
        handle = self._OpenProcess(0x1000 | 0x0001 | 0x0100 | 0x00100000, False, pid)
        if not handle:
            raise self._fail("OpenProcess")
        return int(handle)

    def current_process(self) -> int:
        return int(self._GetCurrentProcess())

    def process_image_name(self, process: int) -> str:
        self._bind_process_api()
        ctypes = self._ctypes
        size = self._wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not self._QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            raise self._fail("QueryFullProcessImageNameW")
        return buffer.value

    def process_creation_time(self, process: int) -> int:
        self._bind_process_api()
        ctypes = self._ctypes
        times = [self.FILETIME() for _ in range(4)]
        if not self._GetProcessTimes(process, *(ctypes.byref(item) for item in times)):
            raise self._fail("GetProcessTimes")
        return self._filetime_ns(times[0])

    def resume_process(self, pid: int) -> int:
        self._bind_process_api()
        ctypes = self._ctypes
        snapshot = self._CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if not snapshot or snapshot == self._invalid_handle:
            raise self._fail("CreateToolhelp32Snapshot")
        resumed = 0
        try:
            entry = self.THREADENTRY32()
            entry.dwSize = ctypes.sizeof(entry)
            more = self._Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = self._OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread:
                        raise self._fail("OpenThread")
                    try:
                        if self._ResumeThread(thread) == 0xFFFFFFFF:
                            raise self._fail("ResumeThread")
                        resumed += 1
                    finally:
                        self._CloseHandle(thread)
                more = self._Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self._CloseHandle(snapshot)
        return resumed

    def terminate_process(self, process: int, exit_code: int) -> None:
        self._bind_process_api()
        if not self._TerminateProcess(process, exit_code):
            raise self._fail("TerminateProcess")

    def create_job(self) -> int:
        self._bind_process_api()
        handle = self._CreateJobObjectW(None, None)
        if not handle:
            raise self._fail("CreateJobObjectW")
        return int(handle)

    def set_job_limits(self, job: int, limit_flags: int) -> None:
        self._bind_process_api()
        ctypes = self._ctypes
        info = self.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = limit_flags
        if not self._SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise self._fail("SetInformationJobObject")

    def job_limit_flags(self, job: int) -> int:
        self._bind_process_api()
        ctypes = self._ctypes
        info = self.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not self._QueryInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise self._fail("QueryInformationJobObject")
        return int(info.BasicLimitInformation.LimitFlags)

    def assign_process(self, job: int, process: int) -> None:
        self._bind_process_api()
        if not self._AssignProcessToJobObject(job, process):
            raise self._fail("AssignProcessToJobObject")

    def is_process_in_job(self, process: int, job: int) -> bool:
        self._bind_process_api()
        result = self._wintypes.BOOL()
        if not self._IsProcessInJob(process, job, self._ctypes.byref(result)):
            raise self._fail("IsProcessInJob")
        return bool(result.value)

    def job_active_processes(self, job: int) -> int:
        self._bind_process_api()
        ctypes = self._ctypes
        info = self.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not self._QueryInformationJobObject(
            job, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise self._fail("QueryInformationJobObject")
        return int(info.ActiveProcesses)

    def job_process_ids(self, job: int) -> list[int]:
        """``JobObjectBasicProcessIdList``: the ids of every process in the job."""

        self._bind_process_api()
        ctypes = self._ctypes
        capacity = 256

        class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
            _fields_ = [
                ("NumberOfAssignedProcesses", self._wintypes.DWORD),
                ("NumberOfProcessIdsInList", self._wintypes.DWORD),
                ("ProcessIdList", ctypes.c_size_t * capacity),
            ]

        info = JOBOBJECT_BASIC_PROCESS_ID_LIST()
        if not self._QueryInformationJobObject(
            job, 3, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise self._fail("QueryInformationJobObject")
        if info.NumberOfProcessIdsInList != info.NumberOfAssignedProcesses:
            raise Win32Error(ERROR_MORE_DATA, "QueryInformationJobObject")
        return [int(info.ProcessIdList[index]) for index in range(info.NumberOfProcessIdsInList)]

    def process_working_set_bytes_of(self, process: int) -> int:
        return self._working_set(process)

    def terminate_job(self, job: int, exit_code: int) -> None:
        self._bind_process_api()
        if not self._TerminateJobObject(job, exit_code):
            raise self._fail("TerminateJobObject")

    # ------------------------------------------------ memory and events

    def process_working_set_bytes(self) -> int:
        return self._working_set(self._GetCurrentProcess())

    def _working_set(self, process: Any) -> int:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        function = self._kernel32.K32GetProcessMemoryInfo
        function.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        function.restype = wintypes.BOOL
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not function(process, ctypes.byref(counters), counters.cb):
            raise self._fail("K32GetProcessMemoryInfo")
        return int(counters.WorkingSetSize)

    def available_physical_bytes(self) -> int:
        return int(self._memory_status().ullAvailPhys)

    def total_physical_bytes(self) -> int:
        return int(self._memory_status().ullTotalPhys)

    def _memory_status(self) -> Any:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        function = self._kernel32.GlobalMemoryStatusEx
        function.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
        function.restype = wintypes.BOOL
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not function(ctypes.byref(status)):
            raise self._fail("GlobalMemoryStatusEx")
        return status

    def _bind_event_api(self) -> None:
        if getattr(self, "_event_api_bound", False):
            return
        wintypes = self._wintypes
        kernel32 = self._kernel32
        self._OpenEventW = kernel32.OpenEventW
        self._OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self._OpenEventW.restype = wintypes.HANDLE
        self._WaitForSingleObject = kernel32.WaitForSingleObject
        self._WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._WaitForSingleObject.restype = wintypes.DWORD
        self._event_api_bound = True

    def open_event(self, name: str) -> int:
        self._bind_event_api()
        # SYNCHRONIZE | EVENT_MODIFY_STATE
        handle = self._OpenEventW(0x00100000 | 0x0002, False, name)
        if not handle:
            raise self._fail("OpenEventW")
        return int(handle)

    def create_event(self, name: str) -> tuple[int, bool]:
        """Manual-reset, initially unsignalled named event; (handle, existed)."""

        self._bind_event_api()
        wintypes = self._wintypes
        function = self._kernel32.CreateEventW
        function.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        function.restype = wintypes.HANDLE
        handle = function(None, True, False, name)
        existed = self._ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        if not handle:
            raise self._fail("CreateEventW")
        return int(handle), existed

    def set_event(self, event: int) -> None:
        wintypes = self._wintypes
        function = self._kernel32.SetEvent
        function.argtypes = [wintypes.HANDLE]
        function.restype = wintypes.BOOL
        if not function(event):
            raise self._fail("SetEvent")

    def wait_event(self, event: int, timeout_ms: int) -> bool:
        self._bind_event_api()
        status = self._WaitForSingleObject(event, timeout_ms)
        if status == 0:
            return True
        if status == 0x00000102:
            return False
        raise self._fail("WaitForSingleObject")


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
