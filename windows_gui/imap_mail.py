"""Configurable read-only IMAP summary backends for supported mailboxes."""

from __future__ import annotations

import hashlib
import imaplib
import os
import socket
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
from typing import Any, Callable, Protocol

from .mail_backends import (
    BackendEmail,
    BackendStatus,
    MailBackendResult,
    MailDraftRequest,
    MailDraftResult,
    MailSearchQuery,
    MailSearchResult,
    MailSendRequest,
    MailSendResult,
)


QQ_IMAP_HOST = 'imap.qq.com'
QQ_IMAP_PORT = 993
QQ_IMAP_CREDENTIAL_SERVICE = 'AI-Work/windows-gui/mailboxes'
QQ_IMAP_CREDENTIAL_USERNAME = 'qq_mail_imap_authorization_code'
QQ_IMAP_USERNAME_ENVIRONMENT = 'AI_WORK_QQ_IMAP_USERNAME'
BACHELOR_IMAP_HOST = 'imaphz.qiye.163.com'
BACHELOR_IMAP_PORT = 993
BACHELOR_IMAP_CREDENTIAL_SERVICE = 'AI-Work/windows-gui/mailboxes'
BACHELOR_IMAP_CREDENTIAL_USERNAME = 'bachelor_mail_imap_authorization_code'
BACHELOR_IMAP_USERNAME_ENVIRONMENT = 'AI_WORK_BACHELOR_IMAP_USERNAME'
_FETCH_ITEMS = '(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])'
_IMAP_MONTHS = (
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
)


class SecureSecretStore(Protocol):
    def get_secret(self) -> str | None:
        """Return one secret from the operating-system credential store."""


ImapFactory = Callable[[str, int, float, ssl.SSLContext], Any]


@dataclass(frozen=True)
class ImapReadonlyConfig:
    username: str | None
    host: str
    port: int
    mailbox_id: str
    display_name: str
    username_environment: str
    timeout_seconds: float = 10.0

    @property
    def is_configured(self) -> bool:
        return bool(self.username)


@dataclass(frozen=True)
class QqImapConfig(ImapReadonlyConfig):
    username: str | None = None
    host: str = QQ_IMAP_HOST
    port: int = QQ_IMAP_PORT
    mailbox_id: str = 'qq_mail'
    display_name: str = 'QQ IMAP'
    username_environment: str = QQ_IMAP_USERNAME_ENVIRONMENT
    timeout_seconds: float = 10.0

    @classmethod
    def from_environment(cls) -> 'QqImapConfig':
        username = os.environ.get(QQ_IMAP_USERNAME_ENVIRONMENT, '').strip()
        return cls(username=username or None)


@dataclass(frozen=True)
class BachelorImapConfig(ImapReadonlyConfig):
    username: str | None = None
    host: str = BACHELOR_IMAP_HOST
    port: int = BACHELOR_IMAP_PORT
    mailbox_id: str = 'bachelor_mail'
    display_name: str = '本科邮箱 IMAP'
    username_environment: str = BACHELOR_IMAP_USERNAME_ENVIRONMENT
    timeout_seconds: float = 10.0

    @classmethod
    def from_environment(cls) -> 'BachelorImapConfig':
        username = os.environ.get(BACHELOR_IMAP_USERNAME_ENVIRONMENT, '').strip()
        return cls(username=username or None)


def _default_imap_factory(
    host: str, port: int, timeout: float, context: ssl.SSLContext
) -> imaplib.IMAP4_SSL:
    return imaplib.IMAP4_SSL(
        host=host,
        port=port,
        timeout=timeout,
        ssl_context=context,
    )


def _opaque_reference(mailbox_id: str, uid: bytes) -> str:
    digest = hashlib.sha256(mailbox_id.encode('utf-8') + b'|' + uid).hexdigest()
    return f'imap:{digest[:24]}'


def _imap_date(value: date) -> str:
    return f'{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year:04d}'


def _parse_internal_date(metadata: bytes) -> datetime | None:
    parsed = imaplib.Internaldate2tuple(metadata)
    if parsed is None:
        return None
    return datetime(*parsed[:6]).astimezone()


def _parse_fetch_response(
    config: ImapReadonlyConfig, uid: bytes, response: list[Any]
) -> BackendEmail | None:
    metadata = b''
    header_bytes = b''
    for part in response:
        if not isinstance(part, tuple) or len(part) < 2:
            continue
        if isinstance(part[0], bytes):
            metadata += part[0]
        if isinstance(part[1], bytes):
            header_bytes += part[1]
    received = _parse_internal_date(metadata)
    if received is None:
        return None
    message = BytesParser(policy=policy.default).parsebytes(header_bytes)
    sender = str(message.get('From') or '').strip()
    subject = str(message.get('Subject') or '').strip()
    if not sender or not subject:
        return None
    return BackendEmail(
        sender=sender,
        subject=subject,
        time=received.isoformat(),
        summary=f'仅从 {config.display_name} 邮件头元数据生成；未读取正文',
        summary_source='IMAP_HEADER_METADATA',
        read_state_changed=False,
        message_reference=_opaque_reference(config.mailbox_id, uid),
        reference_kind='IMAP_UID_OPAQUE',
    )


@dataclass
class ImapReadonlyBackend:
    config: ImapReadonlyConfig
    secret_store: SecureSecretStore
    imap_factory: ImapFactory = field(default=_default_imap_factory)
    today_factory: Callable[[], date] = field(
        default=lambda: datetime.now().astimezone().date()
    )

    def summarize_today(self, max_emails: int) -> MailBackendResult:
        if not self.config.is_configured:
            return MailBackendResult(
                BackendStatus.IMAP_NOT_CONFIGURED,
                f'{self.config.display_name} 用户名未配置；请设置 '
                f'{self.config.username_environment}',
            )
        try:
            authorization_code = self.secret_store.get_secret()
        except Exception:
            return MailBackendResult(
                BackendStatus.IMAP_NOT_CONFIGURED,
                '无法从 Windows Credential Manager 读取 '
                f'{self.config.display_name} 授权码',
            )
        if not authorization_code:
            return MailBackendResult(
                BackendStatus.IMAP_NOT_CONFIGURED,
                f'{self.config.display_name} 授权码在 Windows Credential Manager 中不可用',
            )

        connection = None
        authenticated = False
        try:
            context = ssl.create_default_context()
            connection = self.imap_factory(
                self.config.host,
                self.config.port,
                self.config.timeout_seconds,
                context,
            )
            status, _ = connection.login(self.config.username, authorization_code)
            if status != 'OK':
                return MailBackendResult(
                    BackendStatus.IMAP_AUTH_FAILED,
                    f'{self.config.display_name} 认证失败；请检查账号、授权码和 IMAP 开关',
                )
            authenticated = True
            status, _ = connection.select('INBOX', readonly=True)
            if status != 'OK':
                return MailBackendResult(
                    BackendStatus.IMAP_PROTOCOL_ERROR,
                    f'{self.config.display_name} 无法以只读方式打开收件箱',
                )

            today = self.today_factory()
            since = _imap_date(today)
            status, search_data = connection.uid('SEARCH', None, 'SINCE', since)
            if status != 'OK' or not search_data:
                return MailBackendResult(
                    BackendStatus.IMAP_PROTOCOL_ERROR,
                    f'{self.config.display_name} UID SEARCH 失败',
                )
            uids = search_data[0].split() if search_data[0] else []
            if not uids:
                return MailBackendResult(
                    BackendStatus.EMPTY_TODAY,
                    f'{self.config.display_name} 已确认收件箱今天没有邮件',
                )

            emails: list[BackendEmail] = []
            parsed_count = 0
            for uid in reversed(uids[-max_emails:]):
                status, fetch_data = connection.uid('FETCH', uid, _FETCH_ITEMS)
                if status != 'OK' or not isinstance(fetch_data, list):
                    continue
                email = _parse_fetch_response(self.config, uid, fetch_data)
                if email is None:
                    continue
                parsed_count += 1
                if datetime.fromisoformat(email.time).date() != today:
                    continue
                emails.append(email)
            if parsed_count == 0:
                return MailBackendResult(
                    BackendStatus.MAIL_ITEMS_NOT_PARSED,
                    f'{self.config.display_name} 找到今日候选邮件，但邮件头或日期无法解析',
                )
            if not emails:
                return MailBackendResult(
                    BackendStatus.EMPTY_TODAY,
                    f'{self.config.display_name} 已解析候选邮件，确认今天没有邮件',
                )
            return MailBackendResult(
                BackendStatus.READY,
                f'{self.config.display_name} 只读检查完成；使用 EXAMINE、UID 和 '
                'BODY.PEEK，未改变已读状态',
                tuple(emails),
            )
        except imaplib.IMAP4.error:
            return MailBackendResult(
                BackendStatus.IMAP_PROTOCOL_ERROR if authenticated
                else BackendStatus.IMAP_AUTH_FAILED,
                f'{self.config.display_name} 协议命令失败' if authenticated
                else f'{self.config.display_name} 认证失败；请检查授权码和 IMAP 开关',
            )
        except (OSError, socket.timeout, ssl.SSLError):
            return MailBackendResult(
                BackendStatus.IMAP_NETWORK_FAILED,
                f'{self.config.display_name} 网络或 TLS 连接失败',
            )
        except Exception as error:
            return MailBackendResult(
                BackendStatus.IMAP_PROTOCOL_ERROR,
                f'{self.config.display_name} 响应解析失败：{type(error).__name__}',
            )
        finally:
            authorization_code = None
            if connection is not None:
                try:
                    connection.logout()
                except Exception:
                    pass

    def search(self, query: MailSearchQuery) -> MailSearchResult:
        return MailSearchResult(
            BackendStatus.FORBIDDEN,
            f'{self.config.display_name} backend 仅用于今日摘要，不执行搜索',
        )

    def create_draft(self, request: MailDraftRequest) -> MailDraftResult:
        return MailDraftResult(
            BackendStatus.FORBIDDEN,
            f'{self.config.display_name} credential 不用于创建草稿',
        )

    def send_draft(self, request: MailSendRequest) -> MailSendResult:
        return MailSendResult(
            BackendStatus.FORBIDDEN,
            f'{self.config.display_name} credential 不用于发送邮件',
        )


class QqImapReadonlyBackend(ImapReadonlyBackend):
    """Backward-compatible QQ Mail specialization."""


class BachelorImapReadonlyBackend(ImapReadonlyBackend):
    """China Communication University NetEase mailbox specialization."""


__all__ = [
    'BACHELOR_IMAP_CREDENTIAL_SERVICE',
    'BACHELOR_IMAP_CREDENTIAL_USERNAME', 'BACHELOR_IMAP_HOST',
    'BACHELOR_IMAP_PORT', 'BACHELOR_IMAP_USERNAME_ENVIRONMENT',
    'BachelorImapConfig', 'BachelorImapReadonlyBackend',
    'ImapReadonlyBackend', 'ImapReadonlyConfig',
    'QQ_IMAP_CREDENTIAL_SERVICE', 'QQ_IMAP_CREDENTIAL_USERNAME',
    'QQ_IMAP_HOST', 'QQ_IMAP_PORT', 'QQ_IMAP_USERNAME_ENVIRONMENT',
    'QqImapConfig', 'QqImapReadonlyBackend', 'SecureSecretStore',
]
