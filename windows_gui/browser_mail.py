"""Read-only browser DOM mailbox adapters over an explicitly enabled CDP endpoint."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable
from urllib.parse import urlparse

from .mail_backends import BackendEmail, BackendStatus, MailBackendResult


_PROVIDER_SELECTORS = {
    "qq_mail": {
        "containers": (
            '[role="main"] [role="list"]',
            '[class*="mailList"]',
            '[class*="mail-list"]',
        ),
        "rows": (
            '[role="listitem"]',
            '[class*="mailItem"]',
            '[class*="mail-item"]',
        ),
    },
    "bachelor_mail": {
        "containers": (
            '#mailList',
            '[role="main"] [role="list"]',
            '[class*="mailList"]',
            '[class*="mail-list"]',
        ),
        "rows": (
            '[role="listitem"]',
            '[class*="mailItem"]',
            '[class*="mail-item"]',
        ),
    },
}


@dataclass(frozen=True)
class BrowserMailboxConfig:
    mailbox_id: str
    profile_directory: str
    service_domains: tuple[str, ...]
    endpoint_environment: str

    @property
    def endpoint(self) -> str | None:
        value = os.environ.get(self.endpoint_environment, '').strip()
        return value or None


PageVerifier = Callable[[], str]
DomReader = Callable[[BrowserMailboxConfig, str, int], dict[str, Any]]


def _is_loopback_endpoint(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == 'http'
        and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
        and port is not None
        and parsed.path in {'', '/'}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _safe_reference(mailbox_id: str, raw_reference: str, item: dict[str, Any]) -> str:
    material = raw_reference or '|'.join(
        str(item.get(key, '')) for key in ('sender', 'subject', 'received_time')
    )
    digest = hashlib.sha256(f'{mailbox_id}|{material}'.encode('utf-8')).hexdigest()
    return f'dom:{digest[:24]}'


def _is_today(value: str, today: date) -> bool:
    normalized = value.strip()
    if not normalized:
        return False
    if any(token in normalized.casefold() for token in ('today', '今天', '今日')):
        return True
    try:
        candidate = normalized[:-1] + '+00:00' if normalized.endswith('Z') else normalized
        return datetime.fromisoformat(candidate).astimezone().date() == today
    except ValueError:
        pass
    return today.isoformat() in normalized or today.strftime('%Y/%m/%d') in normalized


def parse_dom_mailbox_snapshot(
    mailbox_id: str,
    snapshot: dict[str, Any],
    max_emails: int,
    today: date | None = None,
) -> MailBackendResult:
    """Convert sanitized DOM metadata into a backend result without opening mail."""
    if snapshot.get('auth_required'):
        return MailBackendResult(BackendStatus.AUTH_REQUIRED, '邮箱登录状态已失效，需要人工登录')
    if not snapshot.get('list_found'):
        return MailBackendResult(BackendStatus.MAIL_LIST_NOT_FOUND, '浏览器 DOM 中未找到可信邮件列表容器')

    raw_items = snapshot.get('items')
    if not isinstance(raw_items, list):
        return MailBackendResult(BackendStatus.MAIL_ITEMS_NOT_PARSED, '邮件列表存在，但 DOM 返回的数据格式无效')
    if not raw_items:
        if snapshot.get('empty_state_found'):
            return MailBackendResult(BackendStatus.EMPTY_TODAY, '已确认邮件列表为空')
        return MailBackendResult(BackendStatus.MAIL_ITEMS_NOT_PARSED, '邮件列表存在，但未能解析邮件行')

    parsed: list[BackendEmail] = []
    parsed_row_count = 0
    target_date = today or datetime.now().astimezone().date()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        sender = str(item.get('sender') or '').strip()
        subject = str(item.get('subject') or '').strip()
        received_time = str(item.get('received_time') or '').strip()
        if not (sender and subject and received_time):
            continue
        parsed_row_count += 1
        if not _is_today(received_time, target_date):
            continue
        parsed.append(BackendEmail(
            sender=sender,
            subject=subject,
            time=received_time,
            summary='仅从浏览器 DOM 邮件列表元数据生成；未打开正文',
            summary_source='BROWSER_DOM_METADATA',
            read_state_changed=False,
            message_reference=_safe_reference(
                mailbox_id, str(item.get('message_reference') or ''), item
            ),
            reference_kind='BROWSER_DOM_OPAQUE',
        ))
        if len(parsed) >= max_emails:
            break
    if parsed_row_count == 0:
        return MailBackendResult(BackendStatus.MAIL_ITEMS_NOT_PARSED, '邮件列表存在，但邮件行字段无法解析')
    if not parsed:
        return MailBackendResult(BackendStatus.EMPTY_TODAY, '已解析邮件列表，确认没有今日邮件')
    return MailBackendResult(
        BackendStatus.READY,
        '浏览器 DOM 只读检查完成；未点击邮件，未打开正文，未改变已读状态',
        tuple(parsed),
    )


def read_mailbox_dom_with_playwright(
    config: BrowserMailboxConfig, endpoint: str, max_emails: int
) -> dict[str, Any]:
    """Attach to an existing loopback CDP server and return sanitized metadata."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError('Playwright 未安装，浏览器 DOM 后端不可用') from error

    selectors = _PROVIDER_SELECTORS[config.mailbox_id]
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=5000)
        matching_frames = []
        for context in browser.contexts:
            for page in context.pages:
                for frame in page.frames:
                    hostname = (urlparse(frame.url).hostname or '').casefold()
                    if hostname in config.service_domains:
                        matching_frames.append(frame)
        if len(matching_frames) != 1:
            return {'attach_identity_ambiguous': True, 'target_count': len(matching_frames)}
        frame = matching_frames[0]
        payload = frame.evaluate(
                """({ containers, rows, limit }) => {
                    const visible = el => !!(el && el.getClientRects().length);
                    const container = containers.map(s => document.querySelector(s)).find(visible);
                    const bodyText = (document.body?.innerText || '').slice(0, 2000).toLowerCase();
                    const authRequired = /登录|sign in|log in|二维码登录|密码登录/.test(bodyText)
                        && !container;
                    if (!container) return { auth_required: authRequired, list_found: false, items: [] };
                    const candidates = [...new Set(rows.flatMap(s => [...container.querySelectorAll(s)]))]
                        .filter(visible).slice(0, limit * 3);
                    const text = (el, selectors) => {
                        for (const selector of selectors) {
                            const node = el.querySelector(selector);
                            const value = node?.getAttribute('datetime') || node?.textContent;
                            if (value && value.trim()) return value.trim();
                        }
                        return '';
                    };
                    const items = candidates.map((row, index) => ({
                        sender: text(row, ['[data-sender]', '[class*="sender"]', '[class*="from"]']),
                        subject: text(row, ['[data-subject]', '[class*="subject"]', '[class*="title"]']),
                        received_time: text(row, ['time', '[data-time]', '[class*="time"]', '[class*="date"]']),
                        message_reference: row.getAttribute('data-message-id') || row.id || String(index),
                    }));
                    const emptyText = (container.textContent || '').toLowerCase();
                    return {
                        auth_required: false,
                        list_found: true,
                        empty_state_found: candidates.length === 0 && /暂无|没有邮件|no messages|empty/.test(emptyText),
                        items,
                    };
                }""",
                {'containers': selectors['containers'], 'rows': selectors['rows'], 'limit': max_emails},
        )
        return payload if isinstance(payload, dict) else {'list_found': True, 'items': None}


@dataclass
class BrowserDomReadonlyBackend:
    config: BrowserMailboxConfig
    verify_page: PageVerifier
    dom_reader: DomReader = read_mailbox_dom_with_playwright

    def summarize_today(self, max_emails: int) -> MailBackendResult:
        verification = self.verify_page()
        if verification == 'AUTH_REQUIRED':
            return MailBackendResult(BackendStatus.AUTH_REQUIRED, '邮箱登录状态已失效，需要人工登录')
        if verification == 'IDENTITY_MISMATCH':
            return MailBackendResult(BackendStatus.IDENTITY_MISMATCH, 'Edge Profile 与邮箱服务域名不一致')
        if verification != 'READY':
            return MailBackendResult(BackendStatus.BROWSER_BACKEND_NOT_READY, '邮箱窗口或页面尚未就绪')
        endpoint = self.config.endpoint
        if not endpoint:
            return MailBackendResult(BackendStatus.BROWSER_BACKEND_NOT_READY, '未配置现有 Edge 的本机 CDP endpoint')
        if not _is_loopback_endpoint(endpoint):
            return MailBackendResult(BackendStatus.BROWSER_BACKEND_NOT_READY, 'CDP endpoint 必须是带显式端口的无凭证本机 HTTP 回环地址')
        try:
            snapshot = self.dom_reader(self.config, endpoint, max_emails)
        except Exception as error:
            return MailBackendResult(BackendStatus.BROWSER_ATTACH_FAILED, f'浏览器 DOM attach 失败：{type(error).__name__}')
        if snapshot.get('attach_identity_ambiguous'):
            return MailBackendResult(BackendStatus.IDENTITY_MISMATCH, 'CDP 中无法唯一确认目标邮箱页面')
        return parse_dom_mailbox_snapshot(self.config.mailbox_id, snapshot, max_emails)

    def search(self, query: Any) -> Any:
        raise NotImplementedError

    def create_draft(self, request: Any) -> Any:
        raise NotImplementedError

    def send_draft(self, request: Any) -> Any:
        raise NotImplementedError


__all__ = [
    'BrowserDomReadonlyBackend', 'BrowserMailboxConfig',
    'parse_dom_mailbox_snapshot', 'read_mailbox_dom_with_playwright',
]
