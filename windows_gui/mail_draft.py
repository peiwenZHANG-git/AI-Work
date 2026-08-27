"""Unified mailbox draft creation that never sends messages."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from email.utils import parseaddr
from typing import Any, Callable, Iterable

from pywinauto import Desktop

from .mail_backends import (
    BackendStatus,
    EdgeFallbackBackend,
    GraphBackendConfig,
    GraphReadonlyBackend,
    MailDraftRequest,
    MailDraftResult,
    WindowsCredentialManagerTokenStore,
)
from .mail_summary import _ensure_mailbox_page
from .mailboxes import (
    MAILBOX_IDENTITIES,
    MailboxIdentity,
    get_runtime_mailbox_context,
)
from .server import mcp
from .uia import _activate, _escape_type_keys_text, _run_bounded
from .windows import _focus_window_handle


LOGGER = logging.getLogger(__name__)
_MAX_SUBJECT_LENGTH = 998
_MAX_BODY_LENGTH = 100_000
_GRAPH_FALLBACK_STATUSES = {
    BackendStatus.NOT_AUTHENTICATED,
    BackendStatus.TOKEN_EXPIRED,
    BackendStatus.REQUEST_FAILED,
}
_STATUS_MAP = {
    BackendStatus.READY: 'READY',
    BackendStatus.NOT_AUTHENTICATED: 'NOT_READY',
    BackendStatus.TOKEN_EXPIRED: 'NOT_READY',
    BackendStatus.REQUEST_FAILED: 'ERROR',
    BackendStatus.FALLBACK_REQUIRED: 'NOT_READY',
    BackendStatus.FORBIDDEN: 'ERROR',
    BackendStatus.IDENTITY_MISMATCH: 'IDENTITY_MISMATCH',
}
_COMPOSE_SELECTORS = {
    'master_mail': {
        'new': ('New mail', 'New message', '新建邮件', '新邮件'),
        'to': ('To', 'Add recipients', '收件人', '请输入收件人'),
        'subject': ('Add a subject', 'Subject', '添加主题', '主题'),
        'body': ('Message body', 'Body', '邮件正文', '正文'),
        'save': ('Save draft', '保存草稿', '存草稿'),
    },
    'bachelor_mail': {
        'new': ('写邮件', '写信', '新建邮件', 'Compose'),
        'to': ('收件人', 'To', '请输入收件人'),
        'subject': ('主题', 'Subject', '添加主题'),
        'body': ('正文', '邮件正文', 'Body'),
        'save': ('存草稿', '保存草稿', 'Save draft'),
    },
    'qq_mail': {
        'new': ('写信', '写邮件', '新建邮件', 'Compose'),
        'to': ('收件人', 'To', '请输入收件人'),
        'subject': ('主题', 'Subject', '添加主题'),
        'body': ('正文', '邮件正文', 'Body'),
        'save': ('存草稿', '保存草稿', 'Save draft'),
    },
}
_ACTIVATION_TYPES = {'button', 'splitbutton', 'menuitem', 'toolbaritem'}
_INPUT_TYPES = {'edit', 'document', 'custom'}
_MAX_UIA_CONTROLS = 750


def _recipient_address(value: str) -> str:
    address = parseaddr(value.strip())[1].strip()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', address):
        raise ValueError('to must contain one valid email address')
    return address


def _edge_draft_reference(
    identity: MailboxIdentity, request: MailDraftRequest,
) -> str:
    material = '|'.join((
        identity.mailbox_id,
        request.to,
        request.subject,
        request.body,
    )).encode('utf-8')
    return f"edge:{identity.mailbox_id}:draft:{hashlib.sha256(material).hexdigest()}"


def _edge_window(hwnd: int):
    return Desktop(backend='uia').window(handle=hwnd).wrapper_object()


def _control_metadata(control) -> tuple[str, str]:
    info = control.element_info
    return (
        (info.name or '').strip().casefold(),
        (info.control_type or '').strip().casefold(),
    )


def _iter_compose_controls(window):
    for index, control in enumerate(window.descendants()):
        if index >= _MAX_UIA_CONTROLS:
            break
        yield control


def _find_named_control(
    window, names: Iterable[str], accepted_types: set[str],
):
    tokens = tuple(name.casefold() for name in names)
    exact = []
    partial = []
    for control in _iter_compose_controls(window):
        try:
            name, control_type = _control_metadata(control)
            if not name or control_type not in accepted_types:
                continue
            if any(name == token for token in tokens):
                exact.append(control)
            elif any(token in name for token in tokens):
                partial.append(control)
        except Exception:
            continue
    matches = exact or partial
    if not matches:
        raise ValueError('compose control not found: ' + ', '.join(tokens))
    return matches[0]


def _wait_for_named_control(
    window,
    names: Iterable[str],
    accepted_types: set[str],
    timeout: float = 12.0,
):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _find_named_control(window, names, accepted_types)
        except ValueError as error:
            last_error = error
            time.sleep(0.25)
    raise RuntimeError(f'compose control unavailable: {last_error}')


def _set_input_value(control, value: str) -> None:
    control.set_focus()
    control.type_keys('^a{BACKSPACE}')
    control.type_keys(
        _escape_type_keys_text(value), with_spaces=True, pause=0.01,
    )


def _run_edge_compose_flow(
    identity: MailboxIdentity,
    hwnd: int,
    request: MailDraftRequest,
) -> str:
    selectors = _COMPOSE_SELECTORS[identity.mailbox_id]

    def create() -> str:
        window = _edge_window(hwnd)
        new_control = _wait_for_named_control(
            window, selectors['new'], _ACTIVATION_TYPES,
        )
        _activate(new_control)

        to_control = _wait_for_named_control(
            window, selectors['to'], _INPUT_TYPES,
        )
        _set_input_value(to_control, request.to)

        subject_control = _wait_for_named_control(
            window, selectors['subject'], _INPUT_TYPES,
        )
        _set_input_value(subject_control, request.subject)

        body_control = _wait_for_named_control(
            window, selectors['body'], _INPUT_TYPES,
        )
        _set_input_value(body_control, request.body)

        save_control = _wait_for_named_control(
            window, selectors['save'], _ACTIVATION_TYPES,
        )
        _activate(save_control)
        return _edge_draft_reference(identity, request)

    return _run_bounded(create, 30.0, 'creating mailbox draft')


def _create_with_edge(
    identity: MailboxIdentity, request: MailDraftRequest,
) -> MailDraftResult:
    snapshot, state = _ensure_mailbox_page(identity)
    if state == 'IDENTITY_MISMATCH':
        return MailDraftResult(
            BackendStatus.IDENTITY_MISMATCH,
            'Edge Profile 或服务域名与指定邮箱不一致，已停止创建草稿',
        )
    if state != 'READY' or snapshot is None:
        return MailDraftResult(
            BackendStatus.FALLBACK_REQUIRED,
            f'Edge mailbox page is not ready: {state}',
        )

    context = get_runtime_mailbox_context(identity.mailbox_id)
    if context is None or context.hwnd is None:
        return MailDraftResult(
            BackendStatus.IDENTITY_MISMATCH,
            '无法确认已验证邮箱窗口绑定，已停止创建草稿',
        )
    if context.profile_directory != identity.profile_directory:
        return MailDraftResult(
            BackendStatus.IDENTITY_MISMATCH,
            'Edge Profile 绑定与指定邮箱不一致，已停止创建草稿',
        )

    _focus_window_handle(context.hwnd)
    try:
        reference = _run_edge_compose_flow(identity, context.hwnd, request)
    except Exception as error:
        return MailDraftResult(
            BackendStatus.REQUEST_FAILED,
            f'Edge draft creation failed: {type(error).__name__}: {error}',
        )
    return MailDraftResult(
        BackendStatus.READY,
        'Edge 草稿已通过显式存草稿控件保存；未发送邮件',
        reference,
        'EDGE_DRAFT_HASH',
    )


def _graph_backend() -> GraphReadonlyBackend:
    config = GraphBackendConfig.from_environment()
    return GraphReadonlyBackend(
        config=config,
        token_store=WindowsCredentialManagerTokenStore(
            config.token_service, config.token_username,
        ),
    )


def _backend_for_identity(identity: MailboxIdentity) -> Any:
    if identity.mailbox_id == 'master_mail':
        return _graph_backend()
    return EdgeFallbackBackend(
        summarize=lambda: {},
        create_draft_impl=lambda request: _create_with_edge(
            identity, request,
        ),
    )


def _create_draft_mailbox(
    identity: MailboxIdentity, request: MailDraftRequest,
) -> dict[str, Any]:
    if 'DRAFT' not in identity.permissions:
        return {
            'mailbox_id': identity.mailbox_id,
            'display_name': identity.display_name,
            'backend': 'NONE',
            'status': 'ERROR',
            'message': f'{identity.display_name}不允许创建草稿',
            'to': request.to,
            'subject': request.subject,
            'draft_reference': None,
            'reference_kind': None,
            'sent': False,
            'send_attempted': False,
        }

    backend_name = 'EDGE_GUI'
    try:
        if identity.mailbox_id == 'master_mail':
            graph_result = _backend_for_identity(identity).create_draft(request)
            result = graph_result
            if result.status in _GRAPH_FALLBACK_STATUSES:
                LOGGER.warning(
                    '%s: Graph draft unavailable (%s); '
                    'using verified Edge fallback',
                    identity.mailbox_id,
                    result.status.value,
                )
                fallback = _create_with_edge(identity, request)
                result = MailDraftResult(
                    fallback.status,
                    (
                        f'Graph draft unavailable ({graph_result.status.value}); '
                        f'Edge fallback: {fallback.message}'
                    ),
                    fallback.draft_reference,
                    fallback.reference_kind,
                )
            else:
                backend_name = 'GRAPH_API'
        else:
            result = _backend_for_identity(identity).create_draft(request)

        return {
            'mailbox_id': identity.mailbox_id,
            'display_name': identity.display_name,
            'backend': backend_name,
            'status': _STATUS_MAP[result.status],
            'message': result.message,
            'to': request.to,
            'subject': request.subject,
            'draft_reference': result.draft_reference,
            'reference_kind': result.reference_kind,
            'sent': False,
            'send_attempted': False,
        }
    except Exception as error:
        LOGGER.exception('%s: draft creation ERROR', identity.mailbox_id)
        return {
            'mailbox_id': identity.mailbox_id,
            'display_name': identity.display_name,
            'backend': backend_name,
            'status': 'ERROR',
            'message': f'{type(error).__name__}: {error}',
            'to': request.to,
            'subject': request.subject,
            'draft_reference': None,
            'reference_kind': None,
            'sent': False,
            'send_attempted': False,
        }


@mcp.tool()
def create_mail_draft(
    mailbox_id: str,
    to: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    """Create a mailbox draft without sending it or adding attachments."""
    identity = MAILBOX_IDENTITIES.get(mailbox_id)
    if identity is None:
        raise ValueError(f'Unknown mailbox identity: {mailbox_id}')

    recipient = _recipient_address(to)
    normalized_subject = subject.strip()
    normalized_body = body.rstrip()
    if not normalized_subject:
        raise ValueError('subject cannot be empty')
    if len(normalized_subject) > _MAX_SUBJECT_LENGTH:
        raise ValueError(f'subject exceeds {_MAX_SUBJECT_LENGTH} characters')
    if len(normalized_body) > _MAX_BODY_LENGTH:
        raise ValueError(f'body exceeds {_MAX_BODY_LENGTH} characters')

    return _create_draft_mailbox(identity, MailDraftRequest(
        to=recipient,
        subject=normalized_subject,
        body=normalized_body,
    ))


__all__ = ['create_mail_draft']