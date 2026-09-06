"""Bounded local task orchestration over the frozen MCP tool surface."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .activity_history import read_activity_history, record_activity
from .mcp_executor import ChildInterrupted, ExecutorQueueFull
from .task_center import TaskCenter, TaskCenterError
from .workflows import (
    ClarificationRequired, InputTooLarge, Plan, UnsupportedTask,
    WorkflowError, compile_workflow, complete_cleanup_plan,
    complete_workspace_plan,
    route_intent,
)


DEDUPE_SECONDS = 120
TASK_CAPACITY = 50
CONFIRM_TTL_SECONDS = 5 * 60
ERROR_SUMMARIES = {
    'destination_exists': '目标文件已存在，未覆盖。',
    'app_not_installed': '该应用未安装或不在允许列表中。',
    'mail_identity_mismatch': '邮箱身份无法确认，已停止操作。',
    'browser_target_not_found': '页面中没有找到唯一可操作目标。',
    'child_interrupted': '执行进程中断。为避免重复操作，本任务已停止。',
    'history_unavailable': '操作已执行，但活动记录暂时不可用。',
    'clarification_required': '需要补充信息后才能继续。',
    'unsupported_task': '该任务不在当前支持范围内。',
    'input_too_large': '输入内容过长，请缩短后重试。',
    'plan_changed': '任务计划已变化，原确认已失效。',
    'queue_full': '当前任务较多，请稍后重试。',
    'operation_failed': '任务未完成。',
}


@dataclass
class AgentTask:
    task_id: str
    request_fingerprint: str
    workflow: str
    plan: Plan | None
    created_mono: float
    status: str = 'RUNNING'
    code: str = 'task_started'
    summary: str = '任务已开始。'
    completed_steps: int = 0
    confirmation_id: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    history_available: bool = True

    def public(self) -> dict[str, Any]:
        payload = {
            'task_id': self.task_id,
            'workflow': self.workflow,
            'status': self.status,
            'code': self.code,
            'summary': self.summary,
            'completed_steps': self.completed_steps,
            'plan': self.plan.public() if self.plan else None,
            'result': self.result,
        }
        if self.status == 'WAITING_CONFIRMATION':
            payload['confirmation_id'] = self.confirmation_id
            if self.workflow == 'mail_draft' and self.plan:
                step = next(
                    (item for item in self.plan.steps if item.tool == 'create_mail_draft'),
                    None,
                )
                if step is not None:
                    payload['draft_preview'] = {
                        'mailbox_id': step.arguments['mailbox_id'],
                        'to': step.arguments['to'],
                        'subject': step.arguments['subject'],
                        'body': step.arguments['body'],
                        'will_send': False,
                    }
        if not self.history_available:
            payload['warning'] = {
                'code': 'history_unavailable',
                'summary': ERROR_SUMMARIES['history_unavailable'],
            }
        return payload


class AgentOrchestrator:
    """Compile and execute only the five fixed workflows."""

    def __init__(
        self,
        executor,
        *,
        history_writer: Callable[..., bool] = record_activity,
        history_reader: Callable[..., dict[str, Any]] = read_activity_history,
        now_mono: Callable[[], float] = time.monotonic,
        today_factory: Callable[[], str] = lambda: datetime.now().astimezone().date().isoformat(),
        draft_generator: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._executor = executor
        self._history_writer = history_writer
        self._history_reader = history_reader
        self._now = now_mono
        self._today = today_factory
        self._draft_generator = draft_generator or self._generate_mail_draft
        self._lock = threading.Lock()
        self._tasks: dict[str, AgentTask] = {}
        self._fingerprints: dict[str, tuple[str, float]] = {}
        self._confirmations = TaskCenter(
            domains={'workflow'}, action_types={'execute_plan'},
            ttl_seconds=CONFIRM_TTL_SECONDS, max_tasks=8,
            terminal_capacity=TASK_CAPACITY,
        )

    @staticmethod
    def _fingerprint(text: str, slots: dict[str, Any] | None) -> str:
        value = {'text': text.strip(), 'slots': slots or {}}
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')
        ).hexdigest()

    @staticmethod
    def _generate_mail_draft(text: str) -> dict[str, Any]:
        from .mail_assistant import ai_generate_draft
        result = ai_generate_draft(text)
        if not isinstance(result, dict):
            raise WorkflowError()
        return result

    def _prepare_slots(self, text: str, slots: dict[str, Any] | None) -> dict[str, Any] | None:
        intent = route_intent(text)
        if intent != 'mail_draft':
            return slots
        prepared = dict(slots or {})
        if 'mailbox_id' not in prepared:
            found = [name for name in ('master_mail', 'bachelor_mail', 'qq_mail') if name in text]
            if len(found) == 1:
                prepared['mailbox_id'] = found[0]
        if 'to' not in prepared:
            address = re.search(r'(?<![\w@])[^\s<>@]{1,180}@[^\s<>@]{1,180}(?![\w@])', text)
            if address:
                prepared['to'] = address.group(0).rstrip('.,，。;；')
        if 'mailbox_id' not in prepared or 'to' not in prepared:
            raise ClarificationRequired()
        if not all(prepared.get(key) for key in ('subject', 'body')):
            generated = self._draft_generator(text)
            for key in ('subject', 'body'):
                value = generated.get(key)
                if isinstance(value, str) and value.strip():
                    prepared[key] = value
        return prepared

    def _remember(self, task: AgentTask) -> None:
        while len(self._tasks) >= TASK_CAPACITY:
            oldest = next(iter(self._tasks))
            self._tasks.pop(oldest, None)
            self._fingerprints = {
                value: record for value, record in self._fingerprints.items()
                if record[0] != oldest
            }
        self._tasks[task.task_id] = task

    def _purge_fingerprints_locked(self, current: float) -> None:
        self._fingerprints = {
            value: (task_id, stamp)
            for value, (task_id, stamp) in self._fingerprints.items()
            if current - stamp <= DEDUPE_SECONDS and task_id in self._tasks
        }

    def submit(self, text: str, slots: dict[str, Any] | None = None) -> dict[str, Any]:
        fingerprint = self._fingerprint(str(text), slots)
        current = self._now()
        with self._lock:
            self._purge_fingerprints_locked(current)
            prior = self._fingerprints.get(fingerprint)
            if prior and current - prior[1] <= DEDUPE_SECONDS:
                task = self._tasks.get(prior[0])
                if task is not None:
                    return task.public()
        try:
            prepared_slots = self._prepare_slots(text, slots)
            plan = compile_workflow(text, prepared_slots)
        except WorkflowError as error:
            code = error.code if error.code in ERROR_SUMMARIES else 'operation_failed'
            task = AgentTask(
                uuid.uuid4().hex, fingerprint, 'unknown', None, current,
                status='BLOCKED', code=code,
                summary=ERROR_SUMMARIES.get(code, '任务未完成。'),
            )
            with self._lock:
                self._remember(task)
                self._fingerprints[fingerprint] = (task.task_id, current)
            return task.public()
        task = AgentTask(uuid.uuid4().hex, fingerprint, plan.intent, plan, current)
        with self._lock:
            self._purge_fingerprints_locked(current)
            prior = self._fingerprints.get(fingerprint)
            if prior and current - prior[1] <= DEDUPE_SECONDS:
                existing = self._tasks.get(prior[0])
                if existing is not None:
                    return existing.public()
            self._remember(task)
            self._fingerprints[fingerprint] = (task.task_id, current)
        self._record(task, 0, 'STARTED', 'task_started')
        self._run_automatic(task)
        return task.public()

    def _record(self, task: AgentTask, step: int, status: str, code: str, **resource) -> None:
        recorded = self._history_writer(
            task.task_id, task.workflow, step, status, code,
            resource_kind=resource.get('kind', 'none'),
            resource_value=resource.get('value', ''),
        )
        if not recorded:
            task.history_available = False

    @staticmethod
    def _resource(step, result: dict[str, Any]) -> tuple[str, str]:
        if step.tool == 'open_app':
            return 'application', str(step.arguments.get('app') or '')
        if step.tool in {'inspect_path', 'open_path', 'manage_path'}:
            return 'file', str(result.get('path') or '')
        if step.tool == 'create_mail_draft':
            return 'mail', str(step.arguments.get('mailbox_id') or '')
        if 'browser' in step.tool or step.tool == 'download_web_file':
            return 'browser', str(step.arguments.get('url') or '')
        return 'none', ''

    def _fail(self, task: AgentTask, code: str, *, interrupted: bool = False) -> None:
        task.status = 'INTERRUPTED' if interrupted else 'FAILED'
        task.code = code if code in ERROR_SUMMARIES else 'operation_failed'
        task.summary = ERROR_SUMMARIES[task.code]
        self._record(task, min(task.completed_steps + 1, 8), task.status, 'child_interrupted' if interrupted else 'task_failed')

    def _call(
        self, task: AgentTask, step, *, allowed_error_codes=frozenset(),
    ) -> dict[str, Any] | None:
        try:
            result = self._executor.submit(step.tool, step.arguments)
        except ExecutorQueueFull:
            self._fail(task, 'queue_full')
            return None
        except ChildInterrupted:
            self._fail(task, 'child_interrupted', interrupted=True)
            return None
        except Exception:
            self._fail(task, 'operation_failed')
            return None
        if (
            str(result.get('status', '')).lower() in {'error', 'failed'}
            and result.get('code') not in allowed_error_codes
        ):
            self._fail(task, str(result.get('code') or 'operation_failed'))
            return None
        task.completed_steps += 1
        kind, value = self._resource(step, result)
        self._record(task, task.completed_steps, 'RUNNING', 'step_succeeded', kind=kind, value=value)
        return result

    def _run_automatic(self, task: AgentTask) -> None:
        assert task.plan is not None
        # Initial scans shape two workflows, but never choose an ambiguous match.
        first = task.plan.steps[0]
        if task.workflow in {'study_workspace', 'coding_workspace', 'file_cleanup'}:
            result = self._call(task, first)
            if result is None:
                return
            try:
                if task.workflow == 'file_cleanup':
                    destination_result = self._call(
                        task, task.plan.steps[1], allowed_error_codes={'not_found'}
                    )
                    if destination_result is None:
                        return
                    task.plan = complete_cleanup_plan(
                        task.plan, result, destination_result, today=self._today()
                    )
                else:
                    task.plan = complete_workspace_plan(task.plan, result)
            except ClarificationRequired:
                task.status = 'BLOCKED'
                task.code = 'clarification_required'
                task.summary = ERROR_SUMMARIES[task.code]
                self._record(task, task.completed_steps, 'BLOCKED', 'clarification_required')
                return
        for step in task.plan.steps[task.completed_steps:]:
            if not step.automatic:
                self._stage_confirmation(task)
                return
            if self._call(task, step) is None:
                return
        self._succeed(task)

    def _stage_confirmation(self, task: AgentTask) -> None:
        assert task.plan is not None
        confirmation_id = self._confirmations.stage(
            'workflow', 'execute_plan', {
                'task_id': task.task_id, 'plan_fingerprint': task.plan.fingerprint,
            }
        )
        task.confirmation_id = confirmation_id
        task.status = 'WAITING_CONFIRMATION'
        task.code = 'confirmation_required'
        task.summary = '请确认预览中的操作。'
        self._record(task, task.completed_steps, 'WAITING_CONFIRMATION', 'confirmation_required')

    def _succeed(self, task: AgentTask) -> None:
        task.status = 'SUCCEEDED'
        task.code = 'task_succeeded'
        task.summary = '任务已完成。'
        task.result = {
            'sent': False,
            'send_attempted': False,
        } if task.workflow == 'mail_draft' else {}
        self._record(task, min(task.completed_steps, 8), 'SUCCEEDED', 'task_succeeded')

    def _preflight_file_mutation(self, task: AgentTask, step) -> bool:
        request = step.arguments.get('request', {})
        if request.get('operation') not in {'move', 'copy', 'rename'}:
            return True
        source = request.get('source')
        destination = request.get('destination')
        if self._executor.submit('inspect_path', {'request': {'operation': 'stat', 'path': source}}).get('status') != 'ok':
            self._fail(task, 'operation_failed')
            return False
        if destination:
            target = self._executor.submit('inspect_path', {'request': {'operation': 'stat', 'path': destination}})
            if target.get('status') == 'ok':
                self._fail(task, 'destination_exists')
                return False
        return True

    def confirm(self, task_id: str, confirmation_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(task_id))
        if (
            task is None or task.status != 'WAITING_CONFIRMATION'
            or task.plan is None or task.confirmation_id != str(confirmation_id)
        ):
            return {'status': 'FAILED', 'code': 'confirmation_invalid', 'summary': '确认已失效。'}
        try:
            context = self._confirmations.consume(str(confirmation_id))
        except TaskCenterError:
            return {'status': 'FAILED', 'code': 'confirmation_invalid', 'summary': '确认已失效。'}
        if context.get('task_id') != task.task_id or context.get('plan_fingerprint') != task.plan.fingerprint:
            task.status = 'BLOCKED'
            task.code = 'plan_changed'
            task.summary = ERROR_SUMMARIES['plan_changed']
            self._confirmations.complete(str(confirmation_id), success=False)
            self._record(task, task.completed_steps, 'BLOCKED', 'plan_changed')
            return task.public()
        success = True
        for step in task.plan.steps[task.completed_steps:]:
            if task.workflow == 'file_cleanup':
                try:
                    if not self._preflight_file_mutation(task, step):
                        success = False
                        break
                except Exception:
                    self._fail(task, 'operation_failed')
                    success = False
                    break
            if self._call(task, step) is None:
                success = False
                break
        self._confirmations.complete(str(confirmation_id), success=success)
        if success:
            self._succeed(task)
        return task.public()

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(str(task_id))
            return None if task is None else task.public()

    def recent(self) -> dict[str, Any]:
        return self._history_reader(task_limit=50)

    def close(self) -> None:
        self._confirmations.cancel_all_staged()
        self._executor.close()


__all__ = ['AgentOrchestrator', 'AgentTask', 'DEDUPE_SECONDS', 'ERROR_SUMMARIES']
