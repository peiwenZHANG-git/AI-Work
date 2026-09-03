"""Internal unified task and confirmation center for staged side effects.

Phase 1 backs the mail assistant pending-draft lifecycle. All state is
process-local: task references, verified context, and terminal outcomes
never leave the server process. Only consume() returns verified context,
and only to trusted domain code; public views and error messages never
carry task payload content.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

TASK_ID_BYTES = 32
EXPIRY_FIELD = '_expires_at_mono'
DEFAULT_TTL_SECONDS = 15 * 60
DEFAULT_MAX_TASKS = 16
DEFAULT_TERMINAL_CAPACITY = 64

STATE_CREATED = 'CREATED'
STATE_STAGED = 'STAGED'
STATE_CONFIRMED = 'CONFIRMED'
STATE_EXECUTING = 'EXECUTING'
STATE_SUCCEEDED = 'SUCCEEDED'
STATE_FAILED = 'FAILED'
STATE_EXPIRED = 'EXPIRED'
STATE_CANCELLED = 'CANCELLED'

TERMINAL_STATES = frozenset({
    STATE_SUCCEEDED,
    STATE_FAILED,
    STATE_EXPIRED,
    STATE_CANCELLED,
})

DOMAINS = frozenset({'mail'})
ACTION_TYPES = frozenset({'assistant_send_draft'})


class TaskCenterError(Exception):
    """Base class for deterministic task center failures."""


class UnknownDomainError(TaskCenterError):
    """Raised when the task domain is not whitelisted."""


class UnknownActionTypeError(TaskCenterError):
    """Raised when the task action type is not whitelisted."""


class UnknownTaskError(TaskCenterError):
    """Raised when a task reference is not staged or tracked."""


class TaskExpiredError(TaskCenterError):
    """Raised when a task reference exists only as an expired record."""


class TaskConsumedError(TaskCenterError):
    """Raised when a task reference was already consumed or finished."""


@dataclass(frozen=True)
class TaskView:
    task_id: str
    domain: str
    action_type: str
    state: str

    def to_public_dict(self) -> dict[str, str]:
        return {
            'task_id': self.task_id,
            'domain': self.domain,
            'action_type': self.action_type,
            'state': self.state,
        }


@dataclass
class _TaskMeta:
    domain: str
    action_type: str
    state: str


class TaskCenter:
    """Locked, bounded, single-use store for staged server-side tasks.

    Pending storage maps task ids to caller-shaped context dictionaries
    with an injected monotonic expiry field. This mirrors the historical
    pending-draft storage so the mail assistant can delegate without any
    externally observable change. The live ``pending`` dictionary is a
    compatibility surface; all lifecycle changes should go through the
    locked methods below.
    """

    def __init__(
        self,
        *,
        domains: Iterable[str] = DOMAINS,
        action_types: Iterable[str] = ACTION_TYPES,
        now_factory: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] = (
            lambda: secrets.token_urlsafe(TASK_ID_BYTES)
        ),
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_tasks: int = DEFAULT_MAX_TASKS,
        terminal_capacity: int = DEFAULT_TERMINAL_CAPACITY,
    ) -> None:
        self._domains = frozenset(domains)
        self._action_types = frozenset(action_types)
        self._now_factory = now_factory
        self._id_factory = id_factory
        self._ttl_seconds = int(ttl_seconds)
        self._max_tasks = int(max_tasks)
        self._terminal_capacity = int(terminal_capacity)
        if self._ttl_seconds <= 0:
            raise ValueError('ttl_seconds must be positive')
        if self._max_tasks < 1:
            raise ValueError('max_tasks must be at least 1')
        if self._terminal_capacity < 1:
            raise ValueError('terminal_capacity must be at least 1')
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._meta: dict[str, _TaskMeta] = {}
        self._terminal: dict[str, _TaskMeta] = {}

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    @property
    def pending(self) -> dict[str, dict[str, Any]]:
        return self._pending

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _current(self, now: float | None) -> float:
        return self._now_factory() if now is None else float(now)

    def _store_terminal_locked(self, task_id: str, meta: _TaskMeta) -> None:
        while len(self._terminal) >= self._terminal_capacity:
            self._terminal.pop(next(iter(self._terminal)))
        self._terminal[task_id] = meta

    def _fallback_meta_locked(self) -> _TaskMeta:
        return _TaskMeta(
            domain=next(iter(self._domains), 'unknown'),
            action_type=next(iter(self._action_types), 'unknown'),
            state=STATE_STAGED,
        )

    def _purge_expired_locked(self, now: float) -> int:
        expired = [
            task_id
            for task_id, context in self._pending.items()
            if context.get(EXPIRY_FIELD, 0) <= now
        ]
        for task_id in expired:
            self._pending.pop(task_id, None)
            meta = self._meta.pop(task_id, None)
            if meta is not None:
                meta.state = STATE_EXPIRED
                self._store_terminal_locked(task_id, meta)
        # Direct mutation of the compatibility dictionary can leave meta
        # without a pending context; prune those orphans deterministically.
        orphans = [
            task_id for task_id in self._meta if task_id not in self._pending
        ]
        for task_id in orphans:
            self._meta.pop(task_id, None)
        return len(expired)

    def stage(
        self,
        domain: str,
        action_type: str,
        context: dict[str, Any],
        *,
        now: float | None = None,
        ttl_seconds: int | None = None,
        max_items: int | None = None,
    ) -> str:
        """Store one verified context and return its single-use reference."""
        domain = str(domain)
        action_type = str(action_type)
        if domain not in self._domains:
            raise UnknownDomainError(f'unsupported task domain: {domain}')
        if action_type not in self._action_types:
            raise UnknownActionTypeError(
                f'unsupported task action type: {action_type}'
            )
        ttl = self._ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if ttl <= 0:
            raise ValueError('ttl_seconds must be positive')
        capacity = self._max_tasks if max_items is None else int(max_items)
        if capacity < 1:
            raise ValueError('max_items must be at least 1')
        current = self._current(now)
        with self._lock:
            self._purge_expired_locked(current)
            while len(self._pending) >= capacity:
                oldest_id = next(iter(self._pending))
                self._pending.pop(oldest_id)
                self._meta.pop(oldest_id, None)
            while True:
                task_id = self._id_factory()
                if task_id and task_id not in self._pending:
                    break
            stored = dict(context)
            stored[EXPIRY_FIELD] = current + ttl
            self._pending[task_id] = stored
            self._meta[task_id] = _TaskMeta(
                domain=domain,
                action_type=action_type,
                state=STATE_STAGED,
            )
            return task_id

    def consume(
        self, task_id: str, *, now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically confirm and take one staged task exactly once."""
        task_id = str(task_id)
        current = self._current(now)
        with self._lock:
            self._purge_expired_locked(current)
            context = self._pending.pop(task_id, None)
            if context is None:
                terminal = self._terminal.get(task_id)
                if terminal is not None and terminal.state == STATE_EXPIRED:
                    raise TaskExpiredError(f'task reference expired: {task_id}')
                if terminal is not None:
                    raise TaskConsumedError(
                        f'task reference already consumed: {task_id}'
                    )
                raise UnknownTaskError(f'unknown task reference: {task_id}')
            meta = self._meta.pop(task_id, None)
            if meta is None:
                meta = self._fallback_meta_locked()
            meta.state = STATE_CONFIRMED
            meta.state = STATE_EXECUTING
            self._store_terminal_locked(task_id, meta)
            return context

    def complete(self, task_id: str, *, success: bool) -> None:
        """Record the terminal outcome of one executing task."""
        task_id = str(task_id)
        with self._lock:
            meta = self._terminal.get(task_id)
            if meta is None or meta.state != STATE_EXECUTING:
                raise UnknownTaskError(
                    f'task reference is not executing: {task_id}'
                )
            meta.state = STATE_SUCCEEDED if success else STATE_FAILED

    def cancel(self, task_id: str) -> bool:
        """Cancel one staged task; returns False when it is not pending."""
        task_id = str(task_id)
        with self._lock:
            return self._cancel_locked(task_id)

    def _cancel_locked(self, task_id: str) -> bool:
        context = self._pending.pop(task_id, None)
        if context is None:
            return False
        meta = self._meta.pop(task_id, None)
        if meta is None:
            meta = self._fallback_meta_locked()
        meta.state = STATE_CANCELLED
        self._store_terminal_locked(task_id, meta)
        return True

    def cancel_all_staged(self) -> int:
        """Cancel every staged task; returns how many were cancelled."""
        with self._lock:
            task_ids = list(self._pending)
            return sum(1 for task_id in task_ids if self._cancel_locked(task_id))

    def lookup(self, task_id: str) -> TaskView | None:
        """Return the public view of one task without any verified context."""
        task_id = str(task_id)
        with self._lock:
            meta = self._meta.get(task_id)
            if meta is not None and task_id in self._pending:
                return TaskView(
                    task_id=task_id,
                    domain=meta.domain,
                    action_type=meta.action_type,
                    state=meta.state,
                )
            terminal = self._terminal.get(task_id)
            if terminal is not None:
                return TaskView(
                    task_id=task_id,
                    domain=terminal.domain,
                    action_type=terminal.action_type,
                    state=terminal.state,
                )
            return None

    def purge_expired(self, now: float | None = None) -> int:
        """Expire due staged tasks and return how many were removed."""
        current = self._current(now)
        with self._lock:
            return self._purge_expired_locked(current)


__all__ = [
    'ACTION_TYPES', 'DEFAULT_MAX_TASKS', 'DEFAULT_TERMINAL_CAPACITY',
    'DEFAULT_TTL_SECONDS', 'DOMAINS', 'EXPIRY_FIELD', 'STATE_CANCELLED',
    'STATE_CONFIRMED', 'STATE_CREATED', 'STATE_EXECUTING', 'STATE_EXPIRED',
    'STATE_FAILED', 'STATE_STAGED', 'STATE_SUCCEEDED', 'TASK_ID_BYTES',
    'TERMINAL_STATES', 'TaskCenter', 'TaskCenterError', 'TaskConsumedError',
    'TaskExpiredError', 'TaskView', 'UnknownActionTypeError',
    'UnknownDomainError', 'UnknownTaskError',
]
