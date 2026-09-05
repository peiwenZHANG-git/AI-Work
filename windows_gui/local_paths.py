"""Shared local-only path policy and Windows handle leases.

No caller-controlled root configuration, shell expansion, logging or audit payloads.
Read/launch ancestors deny WRITE/DELETE sharing. Mutation ancestors deny DELETE
sharing, and every child is opened relative to its parent handle with
OBJ_DONT_REPARSE. Mutation publishes use a parent-relative native handle rename.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import re
from typing import Iterator
import uuid

import win32file
from win32com.shell import shell


MAX_PATH_CHARS = 240
REPARSE = 0x400
DIRECTORY = 0x10
READ_ATTRIBUTES = 0x80
DELETE_ACCESS = 0x10000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
SHARE_READ = 1
OPEN_EXISTING = 3
CREATE_NEW = 1
OPEN_FLAGS = 0x02200000  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT

_FOLDERS = {
    'Downloads': '{374DE290-123F-4565-9164-39C4925E467B}',
    'Documents': '{FDD39AD0-238F-46AF-ADB4-6C85480369C7}',
    'LocalAppData': '{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}',
    'ProgramFiles': '{905E63B6-C1BF-494E-B29C-65B732D3D21A}',
    'ProgramFilesX86': '{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}',
}
_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', re.I)


class PathError(Exception):
    """Only fixed codes are allowed across the public boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def error_result(error: Exception) -> dict:
    if isinstance(error, PathError):
        code = error.code
    else:
        number = getattr(error, 'winerror', None)
        if number is None and getattr(error, 'args', ()):
            number = error.args[0]
        code = {
            2: 'not_found', 3: 'not_found', 5: 'permission_denied',
            17: 'cross_volume_not_supported', 32: 'file_busy', 33: 'file_busy',
            80: 'destination_exists', 183: 'destination_exists',
            112: 'disk_full',
        }.get(number, 'operation_failed') if isinstance(number, int) else 'operation_failed'
    return {'status': 'error', 'code': code}


def known_folder(name: str) -> Path:
    # pywin32 uses SHGetKnownFolderPath, including redirected Known Folders.
    return Path(shell.SHGetKnownFolderPath(_FOLDERS[name], 0, None))


def validate_basename(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 180
            or value in {'.', '..'} or value.endswith((' ', '.'))
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in value for c in '<>:"/\\|?*%~')
            or '$' in value or _RESERVED.match(value)):
        raise PathError('invalid_path')
    return value


def lexical_absolute(value: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        raise PathError('invalid_path')
    normalized = value.replace('/', '\\')
    p = PureWindowsPath(normalized)
    if not re.fullmatch(r'[A-Za-z]:', p.drive) or p.root != '\\':
        raise PathError('invalid_path')
    # Reject traversal rather than interpreting it. Check raw components first.
    tail = normalized[3:]
    if tail.endswith('\\'):
        tail = tail[:-1]
    if tail:
        for component in tail.split('\\'):
            validate_basename(component)
    return Path(str(p))


@dataclass(frozen=True)
class Target:
    path: Path
    label: str


class PathPolicy:
    def __init__(self, roots: dict[str, Path] | None = None):
        # Injection is internal for tests/smoke, never a tool argument or env var.
        self.roots = roots if roots is not None else {
            name: known_folder(name) for name in ('Downloads', 'Documents')
        }

    def target(self, value: str) -> Target:
        if not isinstance(value, str) or len(value) > MAX_PATH_CHARS:
            raise PathError('invalid_path')
        pieces = value.replace('\\', '/').split('/')
        alias = next((n for n in self.roots if n.casefold() == pieces[0].casefold()), None)
        if alias:
            for part in pieces[1:]:
                validate_basename(part)
            absolute = str(self.roots[alias].joinpath(*pieces[1:]))
        else:
            absolute = value
        path = lexical_absolute(absolute)
        for name, root in self.roots.items():
            canonical_root = lexical_absolute(str(root))
            try:
                relative = path.relative_to(canonical_root)
            except ValueError:
                continue
            # Anchor the actual path spelling to the configured root, not the
            # caller's case-folded prefix (important for case-sensitive NTFS).
            return Target(canonical_root / relative,
                          name + (('/' + relative.as_posix()) if relative.parts else ''))
        raise PathError('outside_allowed_roots')


@dataclass
class Lease:
    path: Path
    handle: object
    info: tuple

    @property
    def directory(self) -> bool:
        return bool(self.info[0] & DIRECTORY)

    @property
    def size(self) -> int:
        return (self.info[5] << 32) | self.info[6]

    @property
    def volume(self) -> int:
        return self.info[4]


def _open(path: Path, access: int = READ_ATTRIBUTES, creation: int = OPEN_EXISTING,
          share: int = SHARE_READ):
    # Attribute-only handles are EXEMPT from normal sharing checks on Windows.
    # FILE_READ_DATA / FILE_LIST_DIRECTORY (same bit) makes this a real lease.
    return win32file.CreateFile(str(path), access | 1, share, None, creation, OPEN_FLAGS, None)


class _UnicodeString(ctypes.Structure):
    _fields_ = [('Length', wintypes.USHORT), ('MaximumLength', wintypes.USHORT),
                ('Buffer', wintypes.LPWSTR)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [('Length', wintypes.ULONG), ('RootDirectory', wintypes.HANDLE),
                ('ObjectName', ctypes.POINTER(_UnicodeString)), ('Attributes', wintypes.ULONG),
                ('SecurityDescriptor', ctypes.c_void_p), ('SecurityQualityOfService', ctypes.c_void_p)]


class _IoStatus(ctypes.Structure):
    _fields_ = [('Status', ctypes.c_void_p), ('Information', ctypes.c_size_t)]


def _check_nt(status: int) -> None:
    if status >= 0:
        return
    if status & 0xffffffff in {0xc000050b, 0xc0000280, 0xc0000279}:
        raise PathError('reparse_point_not_supported')
    convert = ctypes.WinDLL('ntdll').RtlNtStatusToDosError
    convert.argtypes = [wintypes.LONG]
    convert.restype = wintypes.ULONG
    raise ctypes.WinError(convert(status))


def open_relative(parent_handle, name: str, *, access: int = READ_ATTRIBUTES,
                  share: int = SHARE_READ, create: bool = False, directory: bool = False):
    """NtCreateFile of ONE validated component, anchored to a pinned directory.

    OBJ_DONT_REPARSE forbids redirect processing, including a parent that became
    a junction. OPEN_REPARSE_POINT allows metadata inspection of the leaf itself.
    No pathname retry/fallback is permitted on native failure.
    """
    validate_basename(name)
    buffer = ctypes.create_unicode_buffer(name)
    length = len(name.encode('utf-16-le'))
    string = _UnicodeString(length, length + 2, ctypes.cast(buffer, wintypes.LPWSTR))
    attributes = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), int(parent_handle),
                                   ctypes.pointer(string), 0x1040, None, None)
    result = wintypes.HANDLE()
    io = _IoStatus()
    api = ctypes.WinDLL('ntdll').NtCreateFile
    api.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG,
                    ctypes.POINTER(_ObjectAttributes), ctypes.POINTER(_IoStatus), ctypes.c_void_p,
                    wintypes.ULONG, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG,
                    ctypes.c_void_p, wintypes.ULONG]
    api.restype = wintypes.LONG
    options = 0x200000 | 0x20 | (1 if directory else 0)  # no reparse, synchronous
    status = api(ctypes.byref(result), access | 1 | 0x100000, ctypes.byref(attributes),
                 ctypes.byref(io), None, 0x80, share, 2 if create else 1, options, None, 0)
    _check_nt(status)
    import pywintypes
    return pywintypes.HANDLE(result.value)


def _checked(path: Path, handle, *, allow_links: bool = False) -> Lease:
    info = win32file.GetFileInformationByHandle(handle)
    if info[0] & REPARSE:
        raise PathError('reparse_point_not_supported')
    if win32file.GetFileType(handle) != 1:
        raise PathError('unsupported_file_type')
    final = win32file.GetFinalPathNameByHandle(handle, 0)
    if final.startswith('\\\\?\\'):
        final = final[4:]
    if (PureWindowsPath(final) != PureWindowsPath(path) if allow_links else
            PureWindowsPath(final).parts != PureWindowsPath(path).parts):
        raise PathError('path_changed')
    if not (info[0] & DIRECTORY) and info[7] != 1 and not allow_links:
        raise PathError('hardlink_not_supported')
    return Lease(path, handle, info)


@contextmanager
def pinned(path: Path, *, access: int = READ_ATTRIBUTES,
           allow_links: bool = False, mutation: bool = False) -> Iterator[Lease]:
    """Pin every ancestor before opening its child; refuse mapped/network drives."""
    path = lexical_absolute(str(path))
    if win32file.GetDriveType(path.anchor) != 3:
        raise PathError('local_fixed_drive_required')
    handles = []
    try:
        chain = [*reversed(path.parents), path]
        for index, part in enumerate(chain):
            leaf = index == len(chain) - 1
            desired = access if leaf else READ_ATTRIBUTES
            # The leaf may itself be a destination directory. Share WRITE there
            # only for read-attribute access, never for a source content/DELETE lease.
            share = 3 if mutation and (not leaf or access == READ_ATTRIBUTES) else SHARE_READ
            handle = (_open(part, desired, share=share) if index == 0 else
                      open_relative(handles[-1], part.name, access=desired, share=share))
            handles.append(handle)
            lease = _checked(part, handle, allow_links=allow_links)
            if index < len(chain) - 1 and not lease.directory:
                raise PathError('not_directory')
        yield lease
    finally:
        for handle in reversed(handles):
            handle.Close()


def require_file(lease: Lease) -> None:
    if lease.directory:
        raise PathError('regular_file_required')


def require_directory(lease: Lease) -> None:
    if not lease.directory:
        raise PathError('not_directory')


class _RenameInfo(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('RootDirectory', wintypes.HANDLE),
                ('FileNameLength', wintypes.DWORD), ('FileName', wintypes.WCHAR * 1)]


def _set_info(handle, kind: int, buffer, size: int) -> None:
    api = ctypes.WinDLL('kernel32', use_last_error=True).SetFileInformationByHandle
    api.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.restype = wintypes.BOOL
    if not api(int(handle), kind, ctypes.byref(buffer), size):
        raise ctypes.WinError(ctypes.get_last_error())


def rename_handle(handle, parent: Lease, name: str) -> None:
    """Native FileRenameInformation: pinned parent, basename, ReplaceIfExists=FALSE.

    The Win32 absolute-path wrapper reopens directories and conflicts with leases;
    never release a directory lock or fall back to a pathname to make it succeed.
    """
    validate_basename(name)
    encoded = name.encode('utf-16-le')
    size = max(ctypes.sizeof(_RenameInfo), _RenameInfo.FileName.offset + len(encoded) + 2)
    buffer = ctypes.create_string_buffer(size)
    header = _RenameInfo.from_buffer(buffer)
    header.Flags = 0
    header.RootDirectory = int(parent.handle)
    header.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + _RenameInfo.FileName.offset, encoded, len(encoded))
    io = _IoStatus()
    api = ctypes.WinDLL('ntdll').NtSetInformationFile
    api.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IoStatus), ctypes.c_void_p,
                    wintypes.ULONG, ctypes.c_int]
    api.restype = wintypes.LONG
    _check_nt(api(int(handle), ctypes.byref(io), buffer, size, 10))


@contextmanager
def new_temporary(parent: Lease) -> Iterator[Lease]:
    """Only this CREATE_NEW object may be cleaned on failure, via its open handle."""
    path = parent.path / ('.ai-work-' + uuid.uuid4().hex + '.tmp')
    handle = open_relative(parent.handle, path.name,
                           access=GENERIC_READ | GENERIC_WRITE | DELETE_ACCESS, create=True)
    try:
        yield _checked(path, handle)
    except BaseException:
        delete = wintypes.BOOLEAN(True)
        _set_info(handle, 4, delete, ctypes.sizeof(delete))  # FileDispositionInfo
        raise
    finally:
        handle.Close()


def metadata(lease: Lease, label: str) -> dict:
    return {'path': label, 'type': 'directory' if lease.directory else 'file',
            'size': None if lease.directory else lease.size,
            'modified_time': lease.info[3].isoformat()}
