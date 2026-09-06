"""Deterministic intent routing and bounded workflow compilation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .local_paths import known_folder


MAX_INPUT_CHARS = 4000
MAX_STEPS = 8
INTENTS = (
    'study_workspace', 'file_cleanup', 'course_download',
    'coding_workspace', 'mail_draft',
)
ALLOWED_TOOLS = {
    'study_workspace': frozenset({'inspect_path', 'open_path', 'open_app'}),
    'coding_workspace': frozenset({'inspect_path', 'open_path', 'open_app'}),
    'file_cleanup': frozenset({'inspect_path', 'manage_path'}),
    'course_download': frozenset({
        'download_web_file', 'start_browser_session', 'navigate_browser',
        'inspect_browser', 'click_browser_element',
        'download_browser_element', 'stop_browser_session',
    }),
    'mail_draft': frozenset({'create_mail_draft'}),
}
SIDE_EFFECT_TOOLS = frozenset({
    'open_path', 'open_app', 'manage_path', 'download_web_file',
    'start_browser_session', 'navigate_browser', 'click_browser_element',
    'download_browser_element', 'stop_browser_session', 'create_mail_draft',
})
CONFIRMATION_TOOLS = frozenset({
    'manage_path', 'click_browser_element', 'download_browser_element',
    'create_mail_draft',
})
FORBIDDEN_TOOLS = frozenset({'send_mail_draft'})


class WorkflowError(Exception):
    code = 'invalid_workflow'


class UnsupportedTask(WorkflowError):
    code = 'unsupported_task'


class ClarificationRequired(WorkflowError):
    code = 'clarification_required'


class InputTooLarge(WorkflowError):
    code = 'input_too_large'


@dataclass(frozen=True)
class Step:
    tool: str
    arguments: dict[str, Any]
    automatic: bool
    label: str

    @property
    def side_effect(self) -> bool:
        return self.tool in SIDE_EFFECT_TOOLS

    def public(self) -> dict[str, Any]:
        return {
            'automatic': self.automatic,
            'label': self.label,
        }


@dataclass(frozen=True)
class Plan:
    intent: str
    slots: dict[str, Any]
    steps: tuple[Step, ...]
    preview: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fingerprint(self) -> str:
        canonical = {
            'intent': self.intent,
            'slots': self.slots,
            'steps': [
                {'tool': s.tool, 'arguments': s.arguments, 'automatic': s.automatic}
                for s in self.steps
            ],
        }
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode('utf-8')
        ).hexdigest()

    def public(self) -> dict[str, Any]:
        return {
            'intent': self.intent,
            'steps': [step.public() for step in self.steps],
            'preview': list(self.preview),
            'requires_confirmation': any(not step.automatic for step in self.steps),
        }


def _bounded_text(value: Any) -> str:
    if not isinstance(value, str):
        raise UnsupportedTask()
    value = value.strip()
    if len(value) > MAX_INPUT_CHARS:
        raise InputTooLarge()
    if not value:
        raise UnsupportedTask()
    return value


def route_intent(text: str, model_output: Any = None) -> str:
    """Classify only into the fixed local registry; input is always inert data."""
    text = _bounded_text(text)
    lowered = text.casefold()
    matches: list[str] = []
    if any(word in lowered for word in ('草稿', '起草', 'draft')):
        matches.append('mail_draft')
    if any(word in lowered for word in ('整理', '移动', '归档', '放到', 'cleanup')):
        matches.append('file_cleanup')
    elif any(word in lowered for word in ('下载', 'download', '课程网站')):
        matches.append('course_download')
    if (
        any(word in lowered for word in ('代码', 'coding', 'vscode', '编程'))
        or re.search(r'(?:打开|继续|启动|开始)\s*.{0,40}项目', lowered)
    ):
        matches.append('coding_workspace')
    if any(word in lowered for word in ('学习环境', '作业', 'study', '课程文件夹')):
        matches.append('study_workspace')
    # A model is advisory only and can never introduce a new intent or tool.
    if not matches and isinstance(model_output, dict):
        candidate = model_output.get('intent')
        if candidate in INTENTS and set(model_output).issubset({'intent', 'slots'}):
            matches.append(candidate)
    unique = list(dict.fromkeys(matches))
    if len(unique) != 1:
        if len(unique) > 1:
            raise ClarificationRequired()
        raise UnsupportedTask()
    return unique[0]


def _project_name(text: str) -> str:
    patterns = (
        r'(?:打开|继续|开始|我要开始)\s*([\w\-\u4e00-\u9fff]{1,40})\s*(?:学习环境|作业|项目)',
        r'([\w\-\u4e00-\u9fff]{1,40})\s*(?:学习环境|项目|作业)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            if value not in {'我要', '一个', '课程'}:
                return value
    raise ClarificationRequired()


def _safe_url(text: str) -> str:
    match = re.search(r'https://[^\s<>{}\[\]"]{1,1800}', text)
    if not match:
        raise ClarificationRequired()
    url = match.group(0).rstrip('.,，。)）')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ClarificationRequired()
    return url


def _validate_plan(plan: Plan) -> Plan:
    if plan.intent not in INTENTS or not 1 <= len(plan.steps) <= MAX_STEPS:
        raise WorkflowError()
    allowed = ALLOWED_TOOLS[plan.intent]
    for step in plan.steps:
        if step.tool not in allowed or step.tool in FORBIDDEN_TOOLS:
            raise WorkflowError()
        if (
            step.tool in CONFIRMATION_TOOLS and step.automatic
            and not (
                step.tool == 'click_browser_element'
                and step.arguments.get('confirm') is False
            )
        ):
            raise WorkflowError()
    return plan


def compile_workflow(text: str, slots: dict[str, Any] | None = None) -> Plan:
    text = _bounded_text(text)
    supplied = dict(slots or {})
    intent = route_intent(text, supplied.pop('_model', None))
    if intent in {'study_workspace', 'coding_workspace'}:
        if not set(supplied).issubset({'project'}):
            raise WorkflowError()
        name = str(supplied.get('project') or _project_name(text))
        if '/' in name or '\\' in name or not re.fullmatch(r'[\w\-\u4e00-\u9fff ]{1,40}', name):
            raise ClarificationRequired()
        steps = (
            Step('inspect_path', {
                'request': {'operation': 'search', 'path': 'Documents',
                            'sort': 'name', 'limit': 50, 'max_depth': 3}
            }, True, '查找明确的工作目录'),
        )
        return _validate_plan(Plan(intent, {'project': name}, steps))
    if intent == 'file_cleanup':
        if not set(supplied).issubset({'destination'}):
            raise WorkflowError()
        destination = str(supplied.get('destination') or '')
        if not destination:
            match = re.search(r'(?:到|放到)\s*([\w\-\u4e00-\u9fff ]{1,40})', text)
            destination = match.group(1).strip() if match else ''
        if not destination:
            raise ClarificationRequired()
        destination_path = destination if '/' in destination or '\\' in destination else f'Documents/{destination}'
        steps = (
            Step('inspect_path', {
                'request': {'operation': 'search', 'path': 'Downloads',
                            'extension': '.pdf', 'sort': 'modified_desc',
                            'limit': 100, 'max_depth': 1}
            }, True, '扫描下载目录中的 PDF'),
            Step('inspect_path', {
                'request': {'operation': 'stat', 'path': destination_path}
            }, True, '检查目标课程文件夹'),
        )
        return _validate_plan(Plan(intent, {'destination': destination_path}, steps))
    if intent == 'course_download':
        if not set(supplied).issubset({
            'url', 'authenticated', 'target', 'click_target', 'filename'
        }):
            raise WorkflowError()
        url = str(supplied.get('url') or _safe_url(text))
        session = bool(supplied.get('authenticated')) or any(
            word in text.casefold() for word in ('登录态', '登录后', 'authenticated')
        )
        if session:
            target = str(supplied.get('target') or '').strip()
            click_target = str(supplied.get('click_target') or '').strip()
            quoted = re.findall(r'[“"]([^”"]{1,120})[”"]', text)
            if not target and quoted:
                target = quoted[-1].strip()
            if not click_target and len(quoted) >= 2:
                click_target = quoted[-2].strip()
            if not target:
                raise ClarificationRequired()
            steps = [
                Step('start_browser_session', {'headless': False}, True, '启动专用浏览器会话'),
                Step('navigate_browser', {'url': url}, True, '打开课程页面'),
                Step('inspect_browser', {'max_chars': 12000}, True, '检查可见下载目标'),
            ]
            if click_target:
                steps.extend((
                    Step('click_browser_element', {
                        'text': click_target, 'exact': True, 'confirm': False,
                    }, True, '锁定需要确认的页面操作'),
                    Step('click_browser_element', {
                        'text': click_target, 'exact': True, 'confirm': True,
                    }, False, f'点击确认的“{click_target}”'),
                    Step('inspect_browser', {'max_chars': 12000}, True, '重新检查下载目标'),
                ))
            steps.extend((
                Step('download_browser_element', {
                    'text': target, 'destination_directory': str(known_folder('Downloads')),
                    'filename': '', 'exact': True,
                }, False, '下载确认的课程文件'),
                Step('stop_browser_session', {}, True, '关闭专用浏览器会话'),
            ))
            steps = tuple(steps)
        else:
            steps = (Step('download_web_file', {
                'url': url, 'destination_directory': str(known_folder('Downloads')),
                'filename': str(supplied.get('filename') or ''),
                'overwrite': False,
            }, False, '下载公共课程文件（不覆盖）'),)
        hostname = urlsplit(url).hostname or ''
        preview = (f'从 {hostname} 下载到 Downloads（不覆盖）',)
        return _validate_plan(Plan(intent, {'hostname': hostname}, steps, preview))
    if intent == 'mail_draft':
        if not set(supplied).issubset({'mailbox_id', 'to', 'subject', 'body'}):
            raise WorkflowError()
        mailbox = str(supplied.get('mailbox_id') or '')
        recipient = str(supplied.get('to') or '')
        subject = str(supplied.get('subject') or '')
        body = str(supplied.get('body') or '')
        if mailbox not in {'master_mail', 'bachelor_mail', 'qq_mail'}:
            raise ClarificationRequired()
        if (
            not re.fullmatch(r'[^\s@]{1,180}@[^\s@]{1,180}', recipient)
            or not subject.strip() or len(subject) > 255
            or not body.strip() or len(body) > 20000
        ):
            raise ClarificationRequired()
        steps = (Step('create_mail_draft', {
            'mailbox_id': mailbox, 'to': recipient, 'subject': subject, 'body': body,
        }, False, '保存邮件草稿（不会发送）'),)
        return _validate_plan(Plan(intent, {'mailbox_id': mailbox}, steps))
    raise UnsupportedTask()


def complete_workspace_plan(plan: Plan, inspection: dict[str, Any]) -> Plan:
    """Resolve exactly one matching directory without guessing."""
    target = str(plan.slots['project']).casefold()
    matches = []
    for entry in inspection.get('entries', []):
        label = str(entry.get('path') or '')
        if entry.get('type') == 'directory' and label.replace('\\', '/').rsplit('/', 1)[-1].casefold() == target:
            matches.append(label)
    if len(matches) != 1 or inspection.get('partial'):
        raise ClarificationRequired()
    steps = plan.steps + (
        Step('open_path', {'path': matches[0]}, True, '打开工作目录'),
        Step('open_app', {'app': 'vscode'}, True, '打开 VS Code'),
    )
    return _validate_plan(Plan(plan.intent, plan.slots, steps))


def complete_cleanup_plan(
    plan: Plan,
    inspection: dict[str, Any],
    destination_inspection: dict[str, Any],
    *,
    today: str,
) -> Plan:
    """Create a bounded, confirmation-only move plan from a completed scan."""
    if inspection.get('partial'):
        raise ClarificationRequired()
    entries = [
        item for item in inspection.get('entries', [])
        if str(item.get('modified_time') or '').startswith(today)
        and str(item.get('path') or '').lower().endswith('.pdf')
    ]
    destination = str(plan.slots['destination']).strip()
    steps = list(plan.steps)
    previews = []
    destination_status = str(destination_inspection.get('status') or '').lower()
    if destination_status == 'ok':
        if destination_inspection.get('type') != 'directory':
            raise ClarificationRequired()
    elif destination_inspection.get('code') == 'not_found':
        steps.append(Step('manage_path', {
            'request': {'operation': 'mkdir', 'path': destination}
        }, False, f'创建 {destination} 文件夹'))
        previews.append(f'创建 {destination} 文件夹')
    else:
        raise ClarificationRequired()
    for item in entries:
        source = str(item['path'])
        basename = source.replace('\\', '/').rsplit('/', 1)[-1]
        clean_destination = destination.rstrip('/').rstrip('\\')
        target = f'{clean_destination}/{basename}'
        steps.append(Step('manage_path', {
            'request': {'operation': 'move', 'source': source, 'destination': target}
        }, False, f'移动 {basename}'))
        previews.append(f'{basename} → {destination}')
    if len(steps) == 1 or len(steps) > MAX_STEPS:
        raise ClarificationRequired()
    return _validate_plan(Plan(plan.intent, plan.slots, tuple(steps), tuple(previews)))


__all__ = [
    'ALLOWED_TOOLS', 'CONFIRMATION_TOOLS', 'FORBIDDEN_TOOLS', 'INTENTS',
    'InputTooLarge', 'ClarificationRequired', 'MAX_INPUT_CHARS', 'MAX_STEPS',
    'Plan', 'SIDE_EFFECT_TOOLS', 'Step', 'UnsupportedTask', 'WorkflowError',
    'compile_workflow', 'complete_cleanup_plan', 'complete_workspace_plan',
    'route_intent',
]
