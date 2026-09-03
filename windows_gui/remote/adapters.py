"""Remote staging adapters (Phase 3B-4).

Remote devices can only STAGE requests here; execution happens solely on
the local confirmation plane through the domain's own verified chains
(Phase 2 browser confirmation, mail draft staging, confined downloads).
verified_context stays inside this process and is never serialized to a
remote caller.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..task_center import TaskCenter
from .audit import audit_task_hash
from .protocol import IdempotencyCache, InvalidRequestError


REQUEST_TTL_SECONDS = 30 * 60
REQUEST_CAPACITY = 16
DEFAULT_DOWNLOAD_SUBDIR = 'remote-downloads'

COMMAND_TO_ACTION = {
    'browser.request_click': 'remote_click',
    'browser.request_download': 'remote_download',
    'mail.request_draft': 'remote_draft',
}
DOMAIN_FOR_COMMAND = {
    'browser.request_click': 'browser',
    'browser.request_download': 'browser',
    'mail.request_draft': 'mail',
}


class UnknownTaskError(Exception):
    """Unified denial: unknown task, not owned, or not cancellable."""


def _default_download_root() -> Path:
    local = os.environ.get('LOCALAPPDATA')
    base = Path(local) if local else Path.home() / 'AppData' / 'Local'
    return base / 'AI-Work' / DEFAULT_DOWNLOAD_SUBDIR


def _default_browser_click(text: str, exact: bool) -> dict[str, Any]:
    from ..browser_session import _SESSION

    first = _SESSION.call('click', text, exact, False)
    if isinstance(first, dict) and first.get('status') == 'CONFIRMATION_REQUIRED':
        return _SESSION.call('click', text, exact, True)
    return first


def _default_browser_download(
    text: str, exact: bool, filename: str, download_root: Path,
) -> dict[str, Any]:
    from ..browser_session import _SESSION

    return _SESSION.call('download', text, str(download_root), filename, exact)


def _default_mail_draft(
    mailbox_id: str, to: str, subject: str, body: str,
) -> dict[str, Any]:
    from ..mail_assistant import stage_draft_for_mailbox

    return stage_draft_for_mailbox(mailbox_id, to, subject, body)


def _sanitize_summary(command: str, params: dict[str, Any]) -> str:
    def clip(value: Any, limit: int) -> str:
        return ' '.join(str(value or '').split())[:limit]

    if command == 'browser.request_click':
        return f'点击元素：{clip(params.get("text"), 80)}'
    if command == 'browser.request_download':
        name = clip(params.get('filename') or '自动命名', 80)
        return f'下载文件：{clip(params.get("text"), 60)} → remote-downloads/{name}'
    if command == 'mail.request_draft':
        return (
            f"写草稿：{clip(params.get('mailbox_id'), 24)} → "
            f"{clip(params.get('to'), 80)}｜{clip(params.get('subject'), 80)}"
        )
    return '未知请求'


class RemoteAdapters:
    """Staging, ownership, idempotency, and local-approval execution."""

    def __init__(
        self,
        *,
        now_factory: Callable[[], float] = time.monotonic,
        browser_click_executor: Callable[[str, bool], dict[str, Any]] | None = None,
        browser_download_executor: Callable[..., dict[str, Any]] | None = None,
        mail_draft_executor: Callable[..., dict[str, Any]] | None = None,
        download_root: Path | None = None,
        audit: Callable[..., None] | None = None,
    ) -> None:
        self._requests = TaskCenter(
            domains=('browser', 'mail'),
            action_types=tuple(COMMAND_TO_ACTION.values()),
            ttl_seconds=REQUEST_TTL_SECONDS,
            max_tasks=REQUEST_CAPACITY,
            now_factory=now_factory,
        )
        self._now = now_factory
        self._download_root = Path(download_root) if download_root else None
        self._browser_click = browser_click_executor or _default_browser_click
        self._browser_download = (
            browser_download_executor
            or (lambda text, exact, filename: _default_browser_download(
                text, exact, filename, self._download_root(),
            ))
        )
        self._mail_draft = mail_draft_executor or _default_mail_draft
        self._audit = audit or (lambda *args, **kwargs: None)
        self.idempotency = IdempotencyCache(now_factory=now_factory)
        self._lock = threading.Lock()
        self._device_index: dict[str, list[str]] = {}

    # -- staging -------------------------------------------------------

    def stage(
        self,
        *,
        device_id: str,
        request_id: str,
        command: str,
        params: dict[str, Any],
        fingerprint: str,
    ) -> dict[str, Any]:
        cached = self.idempotency.get(device_id, request_id, fingerprint)
        if cached is not None:
            return cached
        action = COMMAND_TO_ACTION.get(command)
        if action is None:
            raise InvalidRequestError('command is not allowlisted')
        context = {
            'device_id': device_id,
            'request_id': request_id,
            'command': command,
            'params': dict(params),
            'summary': _sanitize_summary(command, params),
        }
        task_id = self._requests.stage(
            DOMAIN_FOR_COMMAND[command], action, context,
        )
        with self._lock:
            index = self._device_index.setdefault(device_id, [])
            index.append(task_id)
            del index[:-64]
        self._audit('task_staged', device_id=device_id, task_id=task_id)
        result = {'task_id': task_id, 'status': 'STAGED'}
        self.idempotency.put(device_id, request_id, result, fingerprint)
        return result

    # -- remote device views ---------------------------------------------

    def list_device_tasks(self, device_id: str) -> list[dict[str, Any]]:
        with self._lock:
            task_ids = list(self._device_index.get(device_id, []))
        tasks = []
        for task_id in task_ids:
            view = self._requests.lookup(task_id)
            if view is None:
                continue
            tasks.append({
                'task_id': task_id,
                'action': view.action_type,
                'state': view.state,
            })
        return tasks

    def cancel_device_task(self, device_id: str, task_id: str) -> bool:
        context = self._requests.inspect_staged_context(task_id)
        if context is None or context.get('device_id') != device_id:
            return False
        if not self._requests.cancel(task_id):
            return False
        self._audit('task_cancelled', device_id=device_id, task_id=task_id)
        return True

    # -- local confirmation plane ------------------------------------------

    def local_confirmations(self) -> list[dict[str, Any]]:
        with self._lock:
            task_ids = [
                task_id
                for ids in self._device_index.values()
                for task_id in ids
            ]
        confirmations = []
        for task_id in task_ids:
            view = self._requests.lookup(task_id)
            if view is None or view.state != 'STAGED':
                continue
            context = self._requests.inspect_staged_context(task_id) or {}
            confirmations.append({
                'task_id': task_id,
                'action': view.action_type,
                'summary': context.get('summary', ''),
                'device_id': context.get('device_id', ''),
            })
        return confirmations

    def approve_task(self, task_id: str) -> dict[str, Any]:
        """Consume one staged task and execute it via the domain chain."""
        context = self._requests.consume(task_id)
        command = str(context.get('command') or '')
        params = context.get('params') or {}
        try:
            result = self._execute(command, params)
        except BaseException:
            self._requests.complete(task_id, success=False)
            raise
        self._requests.complete(task_id, success=True)
        return self._sanitize_result(command, result)

    def reject_task(self, task_id: str) -> bool:
        """Local reject/cancel of any staged task, regardless of owner."""
        if not self._requests.cancel(task_id):
            return False
        return True

    def revoke_device_tasks(self, device_id: str) -> int:
        """Cancel every staged task owned by one (revoked) device."""
        with self._lock:
            task_ids = list(self._device_index.get(device_id, []))
        cancelled = 0
        for task_id in task_ids:
            context = self._requests.inspect_staged_context(task_id)
            if context is not None and context.get('device_id') == device_id:
                if self._requests.cancel(task_id):
                    cancelled += 1
        if cancelled:
            self._audit('task_cancelled', device_id=device_id)
        return cancelled

    def cancel_all_staged(self) -> int:
        with self._lock:
            task_ids = [
                task_id
                for ids in self._device_index.values()
                for task_id in ids
            ]
        cancelled = 0
        for task_id in task_ids:
            if self._requests.cancel(task_id):
                cancelled += 1
        return cancelled

    def purge_device(self, device_id: str) -> None:
        self.idempotency.purge_device(device_id)

    # -- internals ---------------------------------------------------------

    def _execute(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if command == 'browser.request_click':
            return self._browser_click(params['text'], params['exact'])
        if command == 'browser.request_download':
            return self._browser_download(
                params['text'], params['exact'], params.get('filename', ''),
            )
        if command == 'mail.request_draft':
            return self._mail_draft(
                params['mailbox_id'], params['to'],
                params['subject'], params['body'],
            )
        raise InvalidRequestError('command is not executable')

    def _sanitize_result(
        self, command: str, result: dict[str, Any],
    ) -> dict[str, Any]:
        if command == 'browser.request_download':
            return {
                key: result.get(key)
                for key in ('status', 'filename', 'size_bytes', 'sha256')
                if key in result
            }
        if command == 'browser.request_click':
            return {'status': result.get('status')}
        return dict(result)
