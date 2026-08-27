"""Backend-neutral mailbox abstractions for summary, search, draft, and send operations."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Protocol

import keyring
from urllib.parse import quote, urlencode

import requests


GRAPH_READ_SCOPE = 'Mail.Read'
GRAPH_SCOPES = (GRAPH_READ_SCOPE,)
GRAPH_MESSAGES_ENDPOINT = 'https://graph.microsoft.com/v1.0/me/messages'
GRAPH_ME_ENDPOINT = 'https://graph.microsoft.com/v1.0/me'
GRAPH_DRAFT_SCOPE = 'Mail.ReadWrite'
GRAPH_DRAFT_SCOPES = (GRAPH_DRAFT_SCOPE,)
GRAPH_SEND_SCOPE = 'Mail.Send'
GRAPH_SEND_SCOPES = (GRAPH_DRAFT_SCOPE, GRAPH_SEND_SCOPE)


class BackendStatus(str, Enum):
    READY = 'READY'
    NOT_AUTHENTICATED = 'NOT_AUTHENTICATED'
    TOKEN_EXPIRED = 'TOKEN_EXPIRED'
    REQUEST_FAILED = 'REQUEST_FAILED'
    FALLBACK_REQUIRED = 'FALLBACK_REQUIRED'
    FORBIDDEN = 'FORBIDDEN'
    IDENTITY_MISMATCH = 'IDENTITY_MISMATCH'
    NOT_CONFIRMED = 'NOT_CONFIRMED'
    DRAFT_NOT_FOUND = 'DRAFT_NOT_FOUND'
    INVALID_DRAFT = 'INVALID_DRAFT'
    DRAFT_MAILBOX_MISMATCH = 'DRAFT_MAILBOX_MISMATCH'
    AUTH_REQUIRED = 'AUTH_REQUIRED'
    MAIL_LIST_NOT_FOUND = 'MAIL_LIST_NOT_FOUND'
    MAIL_ITEMS_NOT_PARSED = 'MAIL_ITEMS_NOT_PARSED'
    EMPTY_TODAY = 'EMPTY_TODAY'
    BROWSER_BACKEND_NOT_READY = 'BROWSER_BACKEND_NOT_READY'
    BROWSER_ATTACH_FAILED = 'BROWSER_ATTACH_FAILED'
    IMAP_NOT_CONFIGURED = 'IMAP_NOT_CONFIGURED'
    IMAP_AUTH_FAILED = 'IMAP_AUTH_FAILED'
    IMAP_NETWORK_FAILED = 'IMAP_NETWORK_FAILED'
    IMAP_PROTOCOL_ERROR = 'IMAP_PROTOCOL_ERROR'


@dataclass(frozen=True)
class BackendEmail:
    sender: str
    subject: str
    time: str
    summary: str
    summary_source: str = 'GRAPH_METADATA'
    read_state_changed: bool = False
    message_reference: str | None = None
    reference_kind: str | None = None

    def as_result(self) -> dict[str, Any]:
        result = {
            'sender': self.sender,
            'subject': self.subject,
            'time': self.time,
            'summary': self.summary,
            'summary_source': self.summary_source,
            'read_state_changed': self.read_state_changed,
        }
        if self.message_reference is not None:
            result['message_reference'] = self.message_reference
            result['reference_kind'] = self.reference_kind
        return result


@dataclass(frozen=True)
class MailBackendResult:
    status: BackendStatus
    message: str
    emails: tuple[BackendEmail, ...] = ()
    legacy_result: dict[str, Any] | None = None


@dataclass(frozen=True)
class MailSearchQuery:
    keyword: str | None = None
    sender: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    max_results: int = 10


@dataclass(frozen=True)
class MailSearchEmail:
    sender: str
    subject: str
    received_time: datetime
    message_reference: str
    reference_kind: str = 'GRAPH_MESSAGE_ID'

    def as_result(self, mailbox_id: str) -> dict[str, Any]:
        return {
            'mailbox_id': mailbox_id,
            'sender': self.sender,
            'subject': self.subject,
            'received_time': self.received_time.isoformat(),
            'message_reference': self.message_reference,
            'reference_kind': self.reference_kind,
        }


@dataclass(frozen=True)
class MailSearchResult:
    status: BackendStatus
    message: str
    emails: tuple[MailSearchEmail, ...] = ()
    legacy_result: dict[str, Any] | None = None


@dataclass(frozen=True)
class MailDraftRequest:
    to: str
    subject: str
    body: str


@dataclass(frozen=True)
class MailDraftResult:
    status: BackendStatus
    message: str
    draft_reference: str | None = None
    reference_kind: str = 'GRAPH_DRAFT_ID'


@dataclass(frozen=True)
class MailSendRequest:
    draft_reference: str
    confirmed: bool


@dataclass(frozen=True)
class MailSendResult:
    status: BackendStatus
    message: str
    recipient: str | None = None
    subject: str | None = None
    sent_reference: str | None = None
    reference_kind: str | None = None
    send_attempted: bool = False


class MailBackend(Protocol):
    def summarize_today(self, max_emails: int) -> MailBackendResult:
        """Return a read-only summary without changing mailbox state."""

    def search(self, query: MailSearchQuery) -> MailSearchResult:
        """Return read-only search results without changing mailbox state."""

    def create_draft(self, request: MailDraftRequest) -> MailDraftResult:
        """Create a draft without sending it or changing message read state."""

    def send_draft(self, request: MailSendRequest) -> MailSendResult:
        """Send an existing draft only after explicit confirmation."""


@dataclass(frozen=True)
class GraphBackendConfig:
    tenant_id: str | None = None
    client_id: str | None = None
    mailbox: str | None = None
    token_service: str = 'AI-Work/windows-gui/mailboxes'
    token_username: str = 'master_mail_graph_access_token'
    endpoint: str = GRAPH_MESSAGES_ENDPOINT
    identity_endpoint: str = GRAPH_ME_ENDPOINT
    draft_select_fields: str = (
        'id,subject,toRecipients,ccRecipients,bccRecipients,'
        'from,sender,isDraft'
    )

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


@dataclass
class WindowsCredentialManagerSecretStore:
    service: str
    username: str

    def get_secret(self) -> str | None:
        secret = keyring.get_password(self.service, self.username)
        return secret if isinstance(secret, str) and secret else None


GraphTransport = Callable[[str, dict[str, str], float], Any]
GraphDraftTransport = Callable[[str, dict[str, str], dict[str, Any], float], Any]
GraphSendTransport = Callable[[str, dict[str, str], float], Any]


@dataclass
class GraphReadonlyBackend:
    config: GraphBackendConfig
    token_store: SecureTokenStore
    transport: GraphTransport = field(
        default=lambda url, headers, timeout: requests.get(
            url, headers=headers, timeout=timeout
        )
    )
    draft_transport: GraphDraftTransport = field(
        default=lambda url, headers, payload, timeout: requests.post(
            url, headers=headers, json=payload, timeout=timeout
        )
    )
    send_transport: GraphSendTransport = field(
        default=lambda url, headers, timeout: requests.post(
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

    def create_draft(self, request: MailDraftRequest) -> MailDraftResult:
        if not self.config.is_configured:
            return MailDraftResult(
                BackendStatus.NOT_AUTHENTICATED,
                'Graph 后端未完成 Azure 应用与 OAuth 配置',
            )

        token = self.token_store.get_access_token()
        if not token:
            return MailDraftResult(
                BackendStatus.NOT_AUTHENTICATED,
                'Graph 访问令牌不可用，需要完成委托登录',
            )

        headers = {'Authorization': f'Bearer {token}'}
        try:
            identity_response = self.transport(
                self.config.identity_endpoint, headers, 10.0,
            )
            identity_status = getattr(identity_response, 'status_code', None)
            if identity_status == 401:
                return MailDraftResult(
                    BackendStatus.TOKEN_EXPIRED,
                    'Graph 访问令牌已失效，需要重新完成委托登录',
                )
            if (
                identity_status is None
                or identity_status < 200
                or identity_status >= 300
            ):
                return MailDraftResult(
                    BackendStatus.REQUEST_FAILED,
                    f"Graph 身份校验失败：HTTP {identity_status or 'unknown'}",
                )
            try:
                identity_payload = identity_response.json()
            except Exception:
                return MailDraftResult(
                    BackendStatus.REQUEST_FAILED,
                    'Graph 身份校验响应不是有效的 JSON 对象',
                )
            actual_identities = {
                str(identity_payload.get(key) or '').casefold()
                for key in ('userPrincipalName', 'mail')
            }
            expected_identity = self.config.mailbox.casefold()
            if expected_identity not in actual_identities:
                return MailDraftResult(
                    BackendStatus.IDENTITY_MISMATCH,
                    'Graph 登录账号与配置的硕士 Outlook 邮箱不一致',
                )

            payload = {
                'message': {
                    'subject': request.subject,
                    'body': {
                        'contentType': 'Text',
                        'content': request.body,
                    },
                    'toRecipients': [{
                        'emailAddress': {'address': request.to},
                    }],
                },
            }
            draft_response = self.draft_transport(
                self.config.endpoint, headers, payload, 10.0,
            )
            draft_status = getattr(draft_response, 'status_code', None)
            if draft_status == 401:
                return MailDraftResult(
                    BackendStatus.TOKEN_EXPIRED,
                    'Graph 访问令牌已失效，需要重新完成委托登录',
                )
            if draft_status is None or draft_status < 200 or draft_status >= 300:
                return MailDraftResult(
                    BackendStatus.REQUEST_FAILED,
                    f"Graph 创建草稿请求失败：HTTP {draft_status or 'unknown'}",
                )
            try:
                draft_payload = draft_response.json()
                draft_id = str(draft_payload.get('id') or '')
            except Exception:
                return MailDraftResult(
                    BackendStatus.REQUEST_FAILED,
                    'Graph 创建草稿响应不是有效的 JSON 对象',
                )
            if not draft_id:
                return MailDraftResult(
                    BackendStatus.REQUEST_FAILED,
                    'Graph 创建草稿响应缺少 draft id',
                )
            return MailDraftResult(
                BackendStatus.READY,
                'Graph 草稿已保存；未发送邮件',
                draft_id,
                'GRAPH_DRAFT_ID',
            )
        except requests.RequestException as error:
            return MailDraftResult(
                BackendStatus.REQUEST_FAILED,
                f'Graph draft request failed: {type(error).__name__}',
            )
        finally:
            del token

    def send_draft(self, request: MailSendRequest) -> MailSendResult:
        if not request.confirmed:
            return MailSendResult(
                BackendStatus.NOT_CONFIRMED,
                '发送已有草稿需要 confirm_send=true 的明确确认',
            )
        if not self.config.is_configured:
            return MailSendResult(
                BackendStatus.NOT_AUTHENTICATED,
                'Graph 后端未完成 Azure 应用与 OAuth 配置',
            )

        token = self.token_store.get_access_token()
        if not token:
            return MailSendResult(
                BackendStatus.NOT_AUTHENTICATED,
                'Graph 访问令牌不可用，需要完成委托登录',
            )

        headers = {'Authorization': f'Bearer {token}'}
        send_attempted = False
        try:
            identity_response = self.transport(
                self.config.identity_endpoint, headers, 10.0,
            )
            identity_status = getattr(identity_response, 'status_code', None)
            if identity_status == 401:
                return MailSendResult(
                    BackendStatus.TOKEN_EXPIRED,
                    'Graph 访问令牌已失效，需要重新完成委托登录',
                )
            if identity_status == 403:
                return MailSendResult(
                    BackendStatus.FORBIDDEN,
                    'Graph 身份校验被拒绝；请检查委托权限',
                )
            if (
                identity_status is None
                or identity_status < 200
                or identity_status >= 300
            ):
                return MailSendResult(
                    BackendStatus.REQUEST_FAILED,
                    f"Graph 身份校验失败：HTTP {identity_status or 'unknown'}",
                )
            try:
                identity_payload = identity_response.json()
            except Exception:
                return MailSendResult(
                    BackendStatus.REQUEST_FAILED,
                    'Graph 身份校验响应不是有效的 JSON 对象',
                )
            actual_identities = {
                str(identity_payload.get(key) or '').casefold()
                for key in ('userPrincipalName', 'mail')
            }
            expected_identity = self.config.mailbox.casefold()
            if expected_identity not in actual_identities:
                return MailSendResult(
                    BackendStatus.IDENTITY_MISMATCH,
                    'Graph 登录账号与配置的硕士 Outlook 邮箱不一致',
                )

            encoded_reference = quote(request.draft_reference, safe='')
            draft_url = (
                f'{self.config.endpoint}/{encoded_reference}?'
                + urlencode({'$select': self.config.draft_select_fields})
            )
            draft_response = self.transport(draft_url, headers, 10.0)
            draft_status = getattr(draft_response, 'status_code', None)
            if draft_status == 401:
                return MailSendResult(
                    BackendStatus.TOKEN_EXPIRED,
                    'Graph 访问令牌已失效，需要重新完成委托登录',
                )
            if draft_status == 403:
                return MailSendResult(
                    BackendStatus.FORBIDDEN,
                    'Graph 读取草稿被拒绝；请检查 Mail.ReadWrite 权限',
                )
            if draft_status == 404:
                return MailSendResult(
                    BackendStatus.DRAFT_NOT_FOUND,
                    '指定 Graph 草稿不存在或已发送',
                )
            if draft_status is None or draft_status < 200 or draft_status >= 300:
                return MailSendResult(
                    BackendStatus.REQUEST_FAILED,
                    f"Graph 读取草稿失败：HTTP {draft_status or 'unknown'}",
                )
            try:
                draft_payload = draft_response.json()
            except Exception:
                return MailSendResult(
                    BackendStatus.REQUEST_FAILED,
                    'Graph 草稿响应不是有效的 JSON 对象',
                )
            try:
                recipient, subject = self._parse_send_draft(draft_payload)
            except ValueError as error:
                return MailSendResult(
                    BackendStatus.INVALID_DRAFT,
                    str(error),
                )
            for owner_field in ('from', 'sender'):
                owner = draft_payload.get(owner_field)
                if not isinstance(owner, dict):
                    continue
                owner_address = owner.get('emailAddress')
                if not isinstance(owner_address, dict):
                    continue
                address = str(owner_address.get('address') or '')
                if address and address.casefold() != expected_identity:
                    return MailSendResult(
                        BackendStatus.IDENTITY_MISMATCH,
                        'Graph 草稿所属账号与配置的硕士 Outlook 邮箱不一致',
                        recipient,
                        subject,
                    )

            send_attempted = True
            send_response = self.send_transport(
                f'{self.config.endpoint}/{encoded_reference}/send',
                headers,
                10.0,
            )
            send_status = getattr(send_response, 'status_code', None)
            if send_status == 401:
                return MailSendResult(
                    BackendStatus.TOKEN_EXPIRED,
                    'Graph 访问令牌已失效，需要重新完成委托登录',
                    recipient,
                    subject,
                    send_attempted=send_attempted,
                )
            if send_status == 403:
                return MailSendResult(
                    BackendStatus.FORBIDDEN,
                    'Graph 发送被拒绝；请确认委托令牌已具备 Mail.Send',
                    recipient,
                    subject,
                    send_attempted=send_attempted,
                )
            if send_status == 404:
                return MailSendResult(
                    BackendStatus.DRAFT_NOT_FOUND,
                    '发送时指定 Graph 草稿不存在或已被发送',
                    recipient,
                    subject,
                    send_attempted=send_attempted,
                )
            if send_status is None or send_status < 200 or send_status >= 300:
                return MailSendResult(
                    BackendStatus.REQUEST_FAILED,
                    f"Graph 发送草稿请求失败：HTTP {send_status or 'unknown'}",
                    recipient,
                    subject,
                    send_attempted=send_attempted,
                )
            return MailSendResult(
                BackendStatus.READY,
                'Graph 已发送指定草稿；Graph send 响应未返回 message id',
                recipient,
                subject,
                send_attempted=send_attempted,
            )
        except requests.RequestException as error:
            return MailSendResult(
                BackendStatus.REQUEST_FAILED,
                f'Graph send request failed: {type(error).__name__}',
                send_attempted=send_attempted,
            )
        finally:
            del token

    def search(self, query: MailSearchQuery) -> MailSearchResult:
        if not self.config.is_configured:
            return MailSearchResult(
                BackendStatus.NOT_AUTHENTICATED,
                'Graph 后端未完成 Azure 应用与 OAuth 配置',
            )

        token = self.token_store.get_access_token()
        if not token:
            return MailSearchResult(
                BackendStatus.NOT_AUTHENTICATED,
                'Graph 访问令牌不可用，需要完成委托登录',
            )

        filters: list[str] = []
        if query.start_time is not None:
            filters.append(
                f'receivedDateTime ge {query.start_time.isoformat()}'
            )
        if query.end_time is not None:
            filters.append(
                f'receivedDateTime le {query.end_time.isoformat()}'
            )
        if query.sender:
            escaped_sender = query.sender.replace("'", "''")
            filters.append(
                '('
                f"contains(sender/emailAddress/name, '{escaped_sender}') or "
                f"contains(sender/emailAddress/address, '{escaped_sender}')"
                ')'
            )
        if query.keyword:
            escaped_keyword = query.keyword.replace("'", "''")
            filters.append(
                '('
                f"contains(subject, '{escaped_keyword}') or "
                f"contains(sender/emailAddress/name, '{escaped_keyword}') or "
                f"contains(sender/emailAddress/address, '{escaped_keyword}')"
                ')'
            )

        params = {
            '$count': 'true',
            '$orderby': 'receivedDateTime desc',
            '$select': 'id,sender,subject,receivedDateTime',
            '$top': query.max_results,
        }
        if filters:
            params['$filter'] = ' and '.join(filters)
        params = urlencode(params)
        request_url = f'{self.config.endpoint}?{params}'
        try:
            response = self.transport(
                request_url,
                {
                    'Authorization': f'Bearer {token}',
                    'ConsistencyLevel': 'eventual',
                },
                10.0,
            )
        except requests.RequestException as error:
            return MailSearchResult(
                BackendStatus.REQUEST_FAILED,
                f'Graph search failed: {type(error).__name__}',
            )
        finally:
            del token

        status_code = getattr(response, 'status_code', None)
        if status_code == 401:
            return MailSearchResult(
                BackendStatus.TOKEN_EXPIRED,
                'Graph 访问令牌已失效，需要重新完成委托登录',
            )
        if status_code is None or status_code < 200 or status_code >= 300:
            return MailSearchResult(
                BackendStatus.REQUEST_FAILED,
                f"Graph 搜索请求失败：HTTP {status_code or 'unknown'}",
            )

        try:
            payload = response.json()
            messages = payload.get('value', [])
            emails = tuple(
                self._parse_search_message(message)
                for message in messages
            )[:query.max_results]
        except Exception:
            return MailSearchResult(
                BackendStatus.REQUEST_FAILED,
                'Graph 搜索响应不是有效的 JSON 对象或邮件元数据',
            )
        return MailSearchResult(
            BackendStatus.READY,
            'Graph READ-only 搜索完成；仅返回列表元数据，未读取正文',
            emails,
        )

    @staticmethod
    def _parse_send_draft(payload: dict[str, Any]) -> tuple[str, str]:
        if not str(payload.get('id') or ''):
            raise ValueError('Graph 草稿元数据缺少 id')
        if payload.get('isDraft') is not True:
            raise ValueError('指定 Graph 邮件不是可发送草稿')
        recipients = payload.get('toRecipients')
        if not isinstance(recipients, list) or len(recipients) != 1:
            raise ValueError('草稿必须且只能包含一个收件人')
        recipient_entry = recipients[0]
        if not isinstance(recipient_entry, dict):
            raise ValueError('草稿收件人格式无效')
        recipient_address = recipient_entry.get('emailAddress')
        if not isinstance(recipient_address, dict):
            raise ValueError('草稿收件人格式无效')
        recipient = str(recipient_address.get('address') or '').strip()
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', recipient):
            raise ValueError('草稿收件人无效')
        subject = str(payload.get('subject') or '').strip()
        if not subject:
            raise ValueError('草稿主题不能为空')
        for recipient_field in ('ccRecipients', 'bccRecipients'):
            if payload.get(recipient_field):
                raise ValueError('安全发送路径不允许抄送或密送收件人')
        return recipient, subject

    @staticmethod
    def _parse_search_message(message: dict[str, Any]) -> MailSearchEmail:
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
        message_id = str(message.get('id') or '')
        if not message_id:
            raise ValueError('Graph message metadata is missing id')
        return MailSearchEmail(
            sender=sender,
            subject=subject,
            received_time=received,
            message_reference=message_id,
            reference_kind='GRAPH_MESSAGE_ID',
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
    search_messages: Callable[[MailSearchQuery], MailSearchResult] | None = None
    create_draft_impl: Callable[[MailDraftRequest], MailDraftResult] | None = None
    send_draft_impl: Callable[[MailSendRequest], MailSendResult] | None = None

    def summarize_today(self, max_emails: int) -> MailBackendResult:
        result = self.summarize()
        return MailBackendResult(
            BackendStatus.READY if result.get('status') == 'READY'
            else BackendStatus.FALLBACK_REQUIRED,
            result.get('message', ''),
            legacy_result=result,
        )


    def search(self, query: MailSearchQuery) -> MailSearchResult:
        if self.search_messages is None:
            return MailSearchResult(
                BackendStatus.FALLBACK_REQUIRED,
                'Edge READ-only 搜索实现不可用',
            )
        return self.search_messages(query)


    def create_draft(self, request: MailDraftRequest) -> MailDraftResult:
        if self.create_draft_impl is None:
            return MailDraftResult(
                BackendStatus.FALLBACK_REQUIRED,
                'Edge 草稿创建实现不可用',
            )
        return self.create_draft_impl(request)

    def send_draft(self, request: MailSendRequest) -> MailSendResult:
        if not request.confirmed:
            return MailSendResult(
                BackendStatus.NOT_CONFIRMED,
                '发送已有草稿需要 confirm_send=true 的明确确认',
            )
        if self.send_draft_impl is None:
            return MailSendResult(
                BackendStatus.FALLBACK_REQUIRED,
                'Edge 发送已有草稿实现不可用',
            )
        return self.send_draft_impl(request)


__all__ = [
    'BackendEmail', 'BackendStatus', 'EdgeFallbackBackend', 'MailSearchEmail',
    'MailSearchQuery', 'MailSearchResult', 'MailDraftRequest',
    'MailDraftResult', 'MailSendRequest', 'MailSendResult',
    'GRAPH_DRAFT_SCOPES', 'GRAPH_SEND_SCOPES',
    'GraphBackendConfig', 'GraphReadonlyBackend', 'MailBackend',
    'MailBackendResult', 'WindowsCredentialManagerTokenStore',
    'WindowsCredentialManagerSecretStore',
]
