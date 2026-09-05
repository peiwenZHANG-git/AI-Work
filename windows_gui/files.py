"""Four-operation local inspection and bounded, non-destructive file management."""

from __future__ import annotations

import codecs
import os
from pathlib import Path
import time
from typing import Literal

import win32file
from pydantic import BaseModel, ConfigDict, Field, SkipValidation, TypeAdapter

from . import local_paths as paths
from .server import mcp


MAX_SCAN_ENTRIES = 10_000
SCAN_SECONDS = 3.0
MAX_TEXT_FILE = 1024 * 1024
MAX_COPY_BYTES = 256 * 1024 * 1024
COPY_SECONDS = 15.0
CHUNK = 64 * 1024


class Request(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)


class StatRequest(Request):
    operation: Literal['stat']
    path: str = Field(min_length=1, max_length=240)


class ListRequest(Request):
    operation: Literal['list']
    path: str = Field(min_length=1, max_length=240)
    extension: str | None = None
    sort: Literal['name', 'modified_desc'] = 'name'
    limit: int = Field(default=100, ge=1, le=200)


class SearchRequest(Request):
    operation: Literal['search']
    path: str = Field(min_length=1, max_length=240)
    extension: str | None = None
    sort: Literal['name', 'modified_desc'] = 'name'
    limit: int = Field(default=100, ge=1, le=200)
    max_depth: int = Field(default=2, ge=0, le=5)


class ReadRequest(Request):
    operation: Literal['read_text']
    path: str = Field(min_length=1, max_length=240)
    encoding: Literal['utf-8', 'utf-16', 'gb18030'] = 'utf-8'
    max_chars: int = Field(default=16_000, ge=1, le=64_000)


InspectRequest = StatRequest | ListRequest | SearchRequest | ReadRequest
_INSPECT = TypeAdapter(InspectRequest)


class MkdirRequest(Request):
    operation: Literal['mkdir']
    path: str = Field(min_length=1, max_length=240)


class CopyMoveRequest(Request):
    operation: Literal['copy', 'move']
    source: str = Field(min_length=1, max_length=240)
    destination: str = Field(min_length=1, max_length=240)


class RenameRequest(Request):
    operation: Literal['rename']
    source: str = Field(min_length=1, max_length=240)
    new_name: str = Field(min_length=1, max_length=180)


ManageRequest = MkdirRequest | CopyMoveRequest | RenameRequest
_MANAGE = TypeAdapter(ManageRequest)


def _request(adapter: TypeAdapter, value):
    # Raw dict also supported by import callers; never return ValidationError inputs.
    try:
        return adapter.validate_python(value)
    except Exception:
        raise paths.PathError('invalid_request') from None


def _extension(value: str | None) -> str | None:
    if value is None:
        return None
    if (not isinstance(value, str) or not 2 <= len(value) <= 12
            or value[0] != '.' or not value[1:].isascii() or not value[1:].isalnum()):
        raise paths.PathError('invalid_filter')
    return value.lower()


def _read(lease: paths.Lease, request: ReadRequest) -> dict:
    paths.require_file(lease)
    if lease.size > MAX_TEXT_FILE:
        raise paths.PathError('text_too_large')
    # Validate the entire bounded file, including any binary/invalid tail beyond
    # the returned character limit. UTF-8 BOM is accepted; UTF-16 requires BOM.
    raw = bytearray()
    while len(raw) <= MAX_TEXT_FILE:
        _, chunk = win32file.ReadFile(lease.handle, min(CHUNK, MAX_TEXT_FILE + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
    if len(raw) > MAX_TEXT_FILE:
        raise paths.PathError('text_too_large')
    encoding = 'utf-8-sig' if request.encoding == 'utf-8' else request.encoding
    if encoding == 'utf-16' and not raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise paths.PathError('invalid_encoding')
    try:
        text = raw.decode(encoding, errors='strict')
    except UnicodeError:
        raise paths.PathError('invalid_encoding') from None
    if any((ord(c) < 32 and c not in '\t\r\n') or ord(c) == 127 for c in text):
        raise paths.PathError('binary_not_supported')
    return {'text': text[:request.max_chars], 'encoding': request.encoding,
            'truncated': len(text) > request.max_chars, 'size': len(raw)}


def _scan(policy: paths.PathPolicy, target: paths.Target, request) -> dict:
    extension = _extension(request.extension)
    max_depth = request.max_depth if isinstance(request, SearchRequest) else 0
    deadline = time.monotonic() + SCAN_SECONDS
    entries, visited, skipped = [], 0, 0
    partial = False

    def walk(directory: Path, depth: int):
        nonlocal visited, skipped, partial
        with paths.pinned(directory) as lease:
            paths.require_directory(lease)
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if visited >= MAX_SCAN_ENTRIES or time.monotonic() >= deadline:
                        partial = True
                        return
                    visited += 1
                    try:
                        child = policy.target(str(directory / entry.name))
                        with paths.pinned(child.path) as item:
                            if extension is None or (not item.directory and child.path.suffix.lower() == extension):
                                entries.append(paths.metadata(item, child.label))
                            if item.directory and depth < max_depth:
                                walk(child.path, depth + 1)
                    except Exception:
                        # Never expose exception paths or turn an unreadable region
                        # into a confident latest-file result.
                        partial = True
                        skipped += 1
                    if visited >= MAX_SCAN_ENTRIES or time.monotonic() >= deadline:
                        partial = True
                        return

    walk(target.path, 0)
    if request.sort == 'modified_desc':
        entries.sort(key=lambda x: x['path'].casefold())
        entries.sort(key=lambda x: x['modified_time'], reverse=True)
    else:
        entries.sort(key=lambda x: (x['path'].casefold(), x['path']))
    return {'entries': entries[:request.limit], 'partial': partial,
            'results_truncated': len(entries) > request.limit,
            'scanned_entries': visited, 'skipped_entries': skipped,
            'matched_entries': len(entries), 'sort': request.sort,
            'max_depth': max_depth, 'scan_complete': not partial,
            'latest_in_scope_verified': request.sort == 'modified_desc' and not partial and bool(entries)}


def inspect(request: InspectRequest, *, policy: paths.PathPolicy | None = None) -> dict:
    try:
        request = _request(_INSPECT, request)
        policy = policy or paths.PathPolicy()
        target = policy.target(request.path)
        if isinstance(request, (ListRequest, SearchRequest)):
            data = _scan(policy, target, request)
        else:
            access = paths.GENERIC_READ if isinstance(request, ReadRequest) else paths.READ_ATTRIBUTES
            with paths.pinned(target.path, access=access) as lease:
                data = (_read(lease, request) if isinstance(request, ReadRequest)
                        else paths.metadata(lease, target.label))
        return {'status': 'partial' if data.get('partial') else 'ok', 'code': 'ok',
                'path': target.label, **data}
    except Exception as error:
        return paths.error_result(error)


def _absent(parent: paths.Lease, name: str) -> None:
    # Preflight only. CREATE_NEW / no-replace rename enforce the actual boundary.
    try:
        handle = paths.open_relative(parent.handle, name, share=7)
    except OSError as error:
        if error.winerror in (2, 3):
            return
        raise
    else:
        handle.Close()
        raise paths.PathError('destination_exists')


def _copy(source: paths.Lease, parent: paths.Lease, destination: Path) -> None:
    if source.size > MAX_COPY_BYTES:
        raise paths.PathError('copy_too_large')
    deadline = time.monotonic() + COPY_SECONDS
    with paths.new_temporary(parent) as temporary:
        total = 0
        while True:
            if time.monotonic() >= deadline:
                raise paths.PathError('operation_timeout')
            _, chunk = win32file.ReadFile(source.handle, CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_COPY_BYTES:
                raise paths.PathError('copy_too_large')
            _, written = win32file.WriteFile(temporary.handle, chunk)
            if written != len(chunk):
                raise paths.PathError('copy_incomplete')
        if total != source.size:
            raise paths.PathError('source_changed')
        win32file.FlushFileBuffers(temporary.handle)
        paths.rename_handle(temporary.handle, parent, destination.name)


def manage(request: ManageRequest, *, policy: paths.PathPolicy | None = None) -> dict:
    try:
        request = _request(_MANAGE, request)
        policy = policy or paths.PathPolicy()
        if isinstance(request, MkdirRequest):
            target = policy.target(request.path)
            # Root creation/alteration is not a management operation.
            if '/' not in target.label:
                raise paths.PathError('destination_exists')
            with paths.pinned(target.path.parent, mutation=True) as parent:
                paths.require_directory(parent)
                _absent(parent, target.path.name)
                handle = paths.open_relative(parent.handle, target.path.name, create=True, directory=True)
                handle.Close()  # one level, atomic FILE_CREATE / fail-if-exists
            return {'status': 'ok', 'code': 'created', 'path': target.label}
        source = policy.target(request.source)
        if isinstance(request, RenameRequest):
            name = paths.validate_basename(request.new_name)
            destination = policy.target(str(source.path.with_name(name)))
        else:
            destination = policy.target(request.destination)
        if '/' not in destination.label:
            raise paths.PathError('destination_exists')
        access = paths.GENERIC_READ if request.operation == 'copy' else paths.READ_ATTRIBUTES | paths.DELETE_ACCESS
        with paths.pinned(source.path, access=access, mutation=True) as source_lease:
            paths.require_file(source_lease)
            with paths.pinned(destination.path.parent, mutation=True) as parent:
                paths.require_directory(parent)
                _absent(parent, destination.path.name)
                if request.operation == 'copy':
                    _copy(source_lease, parent, destination.path)
                else:
                    if source_lease.volume != parent.volume:
                        raise paths.PathError('cross_volume_not_supported')
                    paths.rename_handle(source_lease.handle, parent, destination.path.name)
        return {'status': 'ok', 'code': {'copy': 'copied', 'move': 'moved', 'rename': 'renamed'}[request.operation],
                'source': source.label, 'path': destination.label}
    except Exception as error:
        return paths.error_result(error)


@mcp.tool()
def inspect_path(request: SkipValidation[InspectRequest]) -> dict:
    """Inspect Downloads/Documents: stat/list/search/read_text, strict per-operation request.

    Accept root-relative paths (Downloads/report.pdf) or allowed absolute paths.
    Search sorts the scanned scope before limiting output; partial=true means no
    global latest-file claim. Text is bounded, uncached, and explicitly requested.
    """
    return inspect(request)


@mcp.tool()
def manage_path(request: SkipValidation[ManageRequest]) -> dict:
    """mkdir(path), copy/move(source,destination), rename(source,new_name).

    Downloads/Documents only. Single-level mkdir; ordinary files only for other
    operations. No overwrite/delete/recursive operations. Move is same-volume.
    new_name is a basename, never a destination parent. User-requested actions only.
    """
    return manage(request)
