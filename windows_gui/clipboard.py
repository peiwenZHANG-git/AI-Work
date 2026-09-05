"""Explicit ephemeral Unicode clipboard read/write, with no logs or persistence."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SkipValidation, TypeAdapter
import win32gui

from .server import mcp


MAX_BYTES = 256 * 1024
EXCLUSION_FORMATS = ('ExcludeClipboardContentFromMonitorProcessing',
                     'CanIncludeInClipboardHistory', 'CanUploadToCloudClipboard')


class ClipboardError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ReadClipboard(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    operation: Literal['read']
    max_chars: int = Field(default=16_000, ge=1, le=64_000)


class WriteClipboard(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    operation: Literal['write']
    text: str = Field(max_length=64_000)


ClipboardRequest = ReadClipboard | WriteClipboard
_REQUEST = TypeAdapter(ClipboardRequest)


def _function(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


class NativeClipboard:
    """Immediate HGLOBAL data; ownership window is hidden and never focused."""

    def __init__(self):
        user = ctypes.WinDLL('user32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.open = _function(user, 'OpenClipboard', [wintypes.HWND], wintypes.BOOL)
        self.close = _function(user, 'CloseClipboard', [], wintypes.BOOL)
        self.empty = _function(user, 'EmptyClipboard', [], wintypes.BOOL)
        self.available = _function(user, 'IsClipboardFormatAvailable', [wintypes.UINT], wintypes.BOOL)
        self.get = _function(user, 'GetClipboardData', [wintypes.UINT], wintypes.HANDLE)
        self.set = _function(user, 'SetClipboardData', [wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE)
        self.register = _function(user, 'RegisterClipboardFormatW', [wintypes.LPCWSTR], wintypes.UINT)
        self.allocate = _function(kernel, 'GlobalAlloc', [wintypes.UINT, ctypes.c_size_t], wintypes.HANDLE)
        self.free = _function(kernel, 'GlobalFree', [wintypes.HANDLE], wintypes.HANDLE)
        self.lock = _function(kernel, 'GlobalLock', [wintypes.HANDLE], ctypes.c_void_p)
        self.unlock = _function(kernel, 'GlobalUnlock', [wintypes.HANDLE], wintypes.BOOL)
        self.size = _function(kernel, 'GlobalSize', [wintypes.HANDLE], ctypes.c_size_t)

    @contextmanager
    def session(self, *, write: bool):
        owner = 0
        opened = False
        try:
            if write:
                owner = win32gui.CreateWindowEx(0, 'STATIC', 'AI-Work clipboard owner',
                                                0, 0, 0, 0, 0, -3, 0, 0, None)
            for attempt in range(4):
                if self.open(owner):
                    opened = True
                    break
                if attempt < 3:
                    time.sleep(0.025)
            if not opened:
                raise ClipboardError('clipboard_busy')
            yield
        finally:
            try:
                if opened and not self.close():
                    raise ClipboardError('clipboard_close_failed')
            finally:
                if owner:
                    win32gui.DestroyWindow(owner)

    def read_text(self) -> str:
        if not self.available(13):  # CF_UNICODETEXT; no images/files/HTML
            raise ClipboardError('text_not_available')
        handle = self.get(13)
        if not handle:
            raise ClipboardError('clipboard_unavailable')
        size = self.size(handle)
        if size > MAX_BYTES:
            raise ClipboardError('clipboard_too_large')
        if size < 2 or size % 2:
            raise ClipboardError('invalid_clipboard_text')
        pointer = self.lock(handle)
        if not pointer:
            raise ClipboardError('clipboard_unavailable')
        try:
            raw = ctypes.string_at(pointer, size)
        finally:
            self.unlock(handle)
        end = next((i for i in range(0, size, 2) if raw[i:i + 2] == b'\0\0'), None)
        if end is None:
            raise ClipboardError('invalid_clipboard_text')
        try:
            return raw[:end].decode('utf-16-le', errors='strict')
        except UnicodeError:
            raise ClipboardError('invalid_clipboard_text') from None

    def write_text(self, text: str) -> None:
        # Allocate everything and register all privacy formats BEFORE clearing.
        # Publish exclusion markers BEFORE plaintext, with immediate data only.
        formats = [self.register(name) for name in EXCLUSION_FORMATS]
        if not all(formats):
            raise ClipboardError('privacy_marker_failed')
        data = [(fmt, b'\0\0\0\0') for fmt in formats]
        data.append((13, text.encode('utf-16-le') + b'\0\0'))
        owned = []
        try:
            for fmt, raw in data:
                handle = self.allocate(0x42, len(raw))  # MOVEABLE | ZEROINIT
                if not handle:
                    raise ClipboardError('clipboard_unavailable')
                owned.append([fmt, handle])
                pointer = self.lock(handle)
                if not pointer:
                    raise ClipboardError('clipboard_unavailable')
                try:
                    ctypes.memmove(pointer, raw, len(raw))
                finally:
                    self.unlock(handle)
            if not self.empty():
                raise ClipboardError('clipboard_unavailable')
            for item in owned:
                fmt, handle = item
                if not self.set(fmt, handle):
                    raise ClipboardError('clipboard_write_failed')
                item[1] = None  # OS owns successfully transferred memory
        finally:
            for _, handle in owned:
                if handle:
                    self.free(handle)


def exchange(request: ClipboardRequest, *, backend=None) -> dict:
    try:
        try:
            request = _REQUEST.validate_python(request)
        except Exception:
            raise ClipboardError('invalid_request') from None
        write = isinstance(request, WriteClipboard)
        if write:
            if '\0' in request.text:
                raise ClipboardError('invalid_text')
            try:
                encoded_size = len(request.text.encode('utf-16-le')) + 2
            except UnicodeError:
                raise ClipboardError('invalid_text') from None
            if encoded_size > MAX_BYTES:
                raise ClipboardError('clipboard_too_large')
        backend = backend or NativeClipboard()
        with backend.session(write=write):
            if write:
                backend.write_text(request.text)
                return {'status': 'ok', 'code': 'written', 'characters': len(request.text),
                        'history_excluded': True, 'cloud_sync_excluded': True}
            text = backend.read_text()
            return {'status': 'ok', 'code': 'read', 'text': text[:request.max_chars],
                    'truncated': len(text) > request.max_chars}
    except Exception as error:
        return {'status': 'error', 'code': error.code if isinstance(error, ClipboardError) else 'clipboard_unavailable'}


@mcp.tool()
def clipboard(request: SkipValidation[ClipboardRequest]) -> dict:
    """Explicit Unicode text clipboard read/write; no content logging, audit or persistence.

    read(max_chars<=64000) returns text to the calling client, which may retain it.
    write(text<=64000) replaces clipboard text and excludes OS history/cloud sync.
    No background collection, binary formats, automatic paste or content echo on write.
    """
    return exchange(request)
