"""Single-worker, bounded-queue client for the frozen stdio MCP server."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from .workflows import SIDE_EFFECT_TOOLS


QUEUE_CAPACITY = 8
EXPECTED_TOOLS = frozenset({
    'clipboard', 'get_system_status', 'inspect_path', 'manage_path',
    'open_path', 'open_app', 'click_control', 'click_menu_item',
    'click_mouse', 'click_save_button', 'double_click', 'drag_mouse',
    'focus_and_press', 'focus_window', 'focus_window_and_hotkey',
    'focus_window_and_press', 'focus_window_and_scroll',
    'focus_window_and_type', 'get_mouse_position', 'hotkey',
    'list_controls', 'list_windows', 'move_mouse', 'press_key',
    'right_click', 'screenshot', 'scroll', 'set_save_dialog_filename',
    'type_text', 'open_all_mailboxes', 'summarize_all_mailboxes_today',
    'search_mailboxes', 'create_mail_draft', 'send_mail_draft',
    'open_webpage', 'download_web_file', 'start_browser_session',
    'navigate_browser', 'inspect_browser', 'click_browser_element',
    'download_browser_element', 'stop_browser_session',
})


class ExecutorError(Exception):
    code = 'executor_error'


class ExecutorQueueFull(ExecutorError):
    code = 'queue_full'


class ChildInterrupted(ExecutorError):
    code = 'child_interrupted'

    def __init__(self, *, unknown: bool):
        super().__init__(self.code)
        self.unknown = unknown


class ToolContractMismatch(ExecutorError):
    code = 'tool_contract_mismatch'


@dataclass
class _Job:
    tool: str
    arguments: dict[str, Any]
    future: concurrent.futures.Future


_STOP = object()


def _default_client_factory():
    root = Path(__file__).resolve().parents[1]
    executable = Path(sys.executable)
    if executable.name.casefold() == 'pythonw.exe':
        console_executable = executable.with_name('python.exe')
        if console_executable.is_file():
            executable = console_executable
    transport = StdioTransport(
        command=str(executable),
        args=['-u', str(root / 'windows_gui_mcp.py')],
        cwd=str(root),
        log_file=Path(os.devnull) if sys.stderr is None else None,
    )
    return Client(transport)


class PersistentMcpExecutor:
    """Own exactly one MCP child and serialize all tool calls."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] = _default_client_factory,
        capacity: int = QUEUE_CAPACITY,
    ) -> None:
        if capacity < 1 or capacity > QUEUE_CAPACITY:
            raise ValueError('capacity must be between 1 and 8')
        self._factory = client_factory
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=capacity)
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._closed = False

    def start(self) -> None:
        with self._start_lock:
            if self._closed:
                raise ExecutorError('executor_closed')
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=lambda: asyncio.run(self._run()),
                name='ai-work-mcp-executor', daemon=True,
            )
            self._thread.start()

    def submit(
        self, tool: str, arguments: dict[str, Any] | None = None,
        *, timeout: float = 30.0,
    ) -> dict[str, Any]:
        if tool not in EXPECTED_TOOLS:
            raise ToolContractMismatch()
        self.start()
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            self._queue.put_nowait(_Job(tool, dict(arguments or {}), future))
        except queue.Full:
            raise ExecutorQueueFull() from None
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # The job may be running. Mutation outcome is therefore unknown and
            # must never be replayed by this caller.
            future.cancel()
            raise ChildInterrupted(unknown=tool in SIDE_EFFECT_TOOLS) from None
        if not isinstance(result, dict):
            raise ToolContractMismatch()
        return result

    async def _open_client(self):
        client = self._factory()
        await client.__aenter__()
        try:
            tools = await client.list_tools()
            names = [str(tool.name) for tool in tools]
            if len(names) != 42 or len(set(names)) != 42 or set(names) != EXPECTED_TOOLS:
                raise ToolContractMismatch()
            return client
        except Exception:
            await client.__aexit__(*sys.exc_info())
            raise

    async def _close_client(self, client) -> None:
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass

    @staticmethod
    def _data(result: Any) -> dict[str, Any]:
        data = getattr(result, 'data', None)
        if data is None:
            data = getattr(result, 'structured_content', None)
        if not isinstance(data, dict):
            raise ToolContractMismatch()
        return data

    async def _call(self, client, job: _Job) -> dict[str, Any]:
        return self._data(await client.call_tool(job.tool, job.arguments))

    async def _run(self) -> None:
        client = None
        try:
            while True:
                item = await asyncio.to_thread(self._queue.get)
                if item is _STOP:
                    return
                job: _Job = item
                if job.future.cancelled():
                    continue
                try:
                    if client is None:
                        client = await self._open_client()
                    result = await self._call(client, job)
                except Exception as first_error:
                    await self._close_client(client)
                    client = None
                    if job.tool in SIDE_EFFECT_TOOLS:
                        if not job.future.done():
                            job.future.set_exception(ChildInterrupted(unknown=True))
                        continue
                    try:
                        client = await self._open_client()
                        result = await self._call(client, job)
                    except Exception:
                        await self._close_client(client)
                        client = None
                        if not job.future.done():
                            job.future.set_exception(ChildInterrupted(unknown=False))
                        continue
                if not job.future.done():
                    job.future.set_result(result)
        finally:
            await self._close_client(client)
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, _Job) and not item.future.done():
                    item.future.set_exception(ExecutorError('executor_closed'))

    def close(self, timeout: float = 5.0) -> None:
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                self._queue.put(_STOP, timeout=timeout)
            except queue.Full:
                return
            thread.join(timeout=timeout)


__all__ = [
    'ChildInterrupted', 'EXPECTED_TOOLS', 'ExecutorError',
    'ExecutorQueueFull', 'PersistentMcpExecutor', 'QUEUE_CAPACITY',
    'ToolContractMismatch',
]
