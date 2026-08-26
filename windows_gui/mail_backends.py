"""Backend-neutral mailbox abstractions for summary operations."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Protocol

import keyring
from urllib.parse import urlencode

import requests


GRAPH_READ_SCOPE = 'Mail.Read'
GRAPH_SCOPES = (GRAPH_READ_SCOPE,)
GRAPH_MESSAGES_ENDPOINT = 'https://graph.microsoft.com/v1.0/me/messages'


class BackendStatus(str, Enum):
    READY = 'READY'
    NOT_AUTHENTICATED = 'NOT_AUTHENTICATED'
    TOKEN_EXPIRED = 'TOKEN_EXPIRED'
    REQUEST_FAILED = 'REQUEST_FAILED'
    FALLBACK_REQUIRED = 'FALLBACK_REQUIRED'


@dataclass(frozen=True)
class BackendEmail:
    sender: str
    subject: str
    time: str
    summary: str
    summary_source: str = 'GRAPH_METADATA'
    read_state_changed: bool = False

    def as_result(self) -> dict[str, Any]:
        return {
            'sender': self.sender,
            'subject': self.subject,
            'time': self.time,
            'summary': self.summary,
            'summary_source': self.summary_source,
            'read_state_changed': self.read_state_changed,
        }


@dataclass(frozen=True)
class MailBackendResult:
    status: BackendStatus
    message: str
    emails: tuple[BackendEmail, ...] = ()
    legacy_result: dict[str, Any] | None = None


class MailBackend(Protocol):
    def summarize_today(self, max_emails: int) -> MailBackendResult:
        """Return a read-only summary without changing mailbox state."""


@dataclass(frozen=True)
class GraphBackendConfig:
    tenant_id: str | None = None
    client_id: str | None = None
    mailbox: str | None = None
    token_service: str = 'AI-Work/windows-gui/mailboxes'
    token_username: str = 'master_mail_graph_access_token'
    endpoint: str = GRAPH_MESSAGES_ENDPOINT

    @classmethod
    def from_environment(cls) -> 'GraphBackendConfig':
        return cls(
            tenant_id=os.environ.get('AI_WORK_OUTLOOK_TENANT_ID'),
            client_id=os.environ.get('AI_WORK_OUTLOOK_CLIENT_ID'),
            mailbox=os.environ.get('AI_WORK_OUTLOOK_MAILBOX'),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.mailbox)


class SecureTokenStore(Protocol):
    def get_access_token(self) -> str | None:
        """Return a token from a system-level secret store."""


@dataclass
class WindowsCredentialManagerTokenStore:
    service: str
    username: str

    def get_access_token(self) -> str | None:
        token = keyring.get_password(self.service, self.username)
        return token if isinstance(token, str) and token else None


GraphTransport = Callable[[str, dict[str, str], float], Any]


@dataclass
class GraphReadonlyBackend:
    config: GraphBackendConfig
    token_store: SecureTokenStore
    transport: GraphTransport = field(
        default=lambda url, headers, timeout: requests.get(
            url, headers=headers, timeout=timeout
        )
    )

    def summarize_today(self, max_emails: int) -> MailBackendResult:
        if not self.config.is_configured:
            return MailBackendResult(
                BackendStatus.NOT_AUTHENTICATED,
                'Graph 后端未完成 Azure 应用与 OAuth 配置',
            )

        token = self.token_store.get_access_token()
        if not token:
            return MailBackendResult(
                BackendStatus.NOT_AUTHENTICATED,
                'Graph 访问令牌不可用，需要完成委托登录',
            )

        params = urlencode({
            '$top': max_emails,
            '$select': 'sender,subject,receivedDateTime',
            '$orderby': 'receivedDateTime desc',
        })
        request_url = f"{self.config.endpoint}?{params}"
        try:
            response = self.transport(
                request_url,
                {'Authorization': f'Bearer {token}'},
                10.0,
            )
        except requests.RequestException as error:
            del token
            return MailBackendResult(
                BackendStatus.REQUEST_FAILED,
                f'Graph request failed: {type(error).__name__}',
            )
        del token
        status_code = getattr(response, 'status_code', None)
        if status_code == 401:
            return MailBackendResult(
                BackendStatus.TOKEN_EXPIRED,
                'Graph 访问令牌已失效，需要重新完成委托登录',
            )
        if status_code is None or status_code < 200 or status_code >= 300:
            return MailBackendResult(
                BackendStatus.REQUEST_FAILED,
                f"Graph 请求失败：HTTP {status_code or 'unknown'}"
            )

        try:
            payload = response.json()
            messages = payload.get('value', [])
        except Exception:
            return MailBackendResult(
                BackendStatus.REQUEST_FAILED,
                'Graph 响应不是有效的 JSON 对象',
            )

        emails = tuple(
            self._parse_message(message)
            for message in messages
            if self._is_today(message.get('receivedDateTime', ''))
        )[:max_emails]
        return MailBackendResult(
            BackendStatus.READY,
            'Graph 只读检查完成；未读取正文，未改变已读状态',
            emails,
        )

    @staticmethod
    def _parse_message(message: dict[str, Any]) -> BackendEmail:
        address = message.get('sender', {}).get('emailAddress', {})
        sender = (
            address.get('name')
            or address.get('address')
            or 'Unknown sender'
        )
        subject = message.get('subject') or '(No subject)'
        received = GraphReadonlyBackend._parse_received_datetime(
            message.get('receivedDateTime', '')
        )
        return BackendEmail(
            sender=sender,
            subject=subject,
            time=received.strftime('%Y-%m-%d %H:%M'),
            summary=(
                f"来自{sender}的邮件，主题为《{subject}》。"
                '未读取正文，摘要仅基于 Graph 列表元数据。'
            ),
        )

    @staticmethod
    def _parse_received_datetime(value: str) -> datetime:
        normalized = value[:-1] + '+00:00' if value.endswith('Z') else value
        parsed = datetime.fromisoformat(normalized)
        return parsed.astimezone()

    @staticmethod
    def _is_today(value: str) -> bool:
        try:
            return (
                GraphReadonlyBackend._parse_received_datetime(value).date()
                == datetime.now().astimezone().date()
            )
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class EdgeFallbackBackend:
    summarize: Callable[[], dict[str, Any]]

    def summarize_today(self, max_emails: int) -> MailBackendResult:
        result = self.summarize()
        return MailBackendResult(
            BackendStatus.READY if result.get('status') == 'READY'
            else BackendStatus.FALLBACK_REQUIRED,
            result.get('message', ''),
            legacy_result=result,
        )


__all__ = [
    'BackendEmail', 'BackendStatus', 'EdgeFallbackBackend',
    'GraphBackendConfig', 'GraphReadonlyBackend', 'MailBackend',
    'MailBackendResult', 'WindowsCredentialManagerTokenStore',
]
