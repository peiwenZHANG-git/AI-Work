"""Unified sending of an existing mailbox draft with explicit confirmation."""

from __future__ import annotations

import logging
import re
from typing import Any

from .mail_backends import (
    BackendStatus,
    GraphBackendConfig,
    GraphReadonlyBackend,
    MailSendRequest,
    MailSendResult,
    WindowsCredentialManagerTokenStore,
)
from .mailboxes import MAILBOX_IDENTITIES, MailboxIdentity
from .server import mcp


LOGGER = logging.getLogger(__name__)
_EDGE_DRAFT_REFERENCE = re.compile(
    r'^edge:(?P<mailbox_id>[a-z_-]+):draft:[0-9a-f]{64}$',
    re.IGNORECASE,
)
_STATUS_MAP = {
    BackendStatus.READY: 'READY',
    BackendStatus.NOT_AUTHENTICATED: 'NOT_READY',
    BackendStatus.TOKEN_EXPIRED: 'NOT_READY',
    BackendStatus.REQUEST_FAILED: 'ERROR',
    BackendStatus.FALLBACK_REQUIRED: 'NOT_READY',
    BackendStatus.FORBIDDEN: 'ERROR',
    BackendStatus.IDENTITY_MISMATCH: 'IDENTITY_MISMATCH',
    BackendStatus.NOT_CONFIRMED: 'CONFIRMATION_REQUIRED',
    BackendStatus.DRAFT_NOT_FOUND: 'DRAFT_NOT_FOUND',
    BackendStatus.INVALID_DRAFT: 'INVALID_DRAFT',
    BackendStatus.DRAFT_MAILBOX_MISMATCH: 'DRAFT_MAILBOX_MISMATCH',
}


def _external_result(
    identity: MailboxIdentity,
    backend_name: str,
    result: MailSendResult,
) -> dict[str, Any]:
    return {
        'mailbox_id': identity.mailbox_id,
        'display_name': identity.display_name,
        'backend': backend_name,
        'status': _STATUS_MAP[result.status],
        'message': result.message,
        'recipient': result.recipient,
        'subject': result.subject,
        'sent_reference': result.sent_reference,
        'reference_kind': result.reference_kind,
        'sent': result.status is BackendStatus.READY,
        'send_attempted': result.send_attempted,
    }


def _validation_result(
    identity: MailboxIdentity,
    backend_name: str,
    status: BackendStatus,
    message: str,
) -> dict[str, Any]:
    return _external_result(
        identity,
        backend_name,
        MailSendResult(status, message),
    )


def _graph_backend() -> GraphReadonlyBackend:
    config = GraphBackendConfig.from_environment()
    return GraphReadonlyBackend(
        config=config,
        token_store=WindowsCredentialManagerTokenStore(
            config.token_service,
            config.token_username,
        ),
    )


def _send_existing_draft(
    identity: MailboxIdentity,
    draft_reference: str,
) -> dict[str, Any]:
    edge_match = _EDGE_DRAFT_REFERENCE.fullmatch(draft_reference)
    if edge_match is not None:
        referenced_mailbox = edge_match.group('mailbox_id')
        if referenced_mailbox != identity.mailbox_id:
            return _validation_result(
                identity,
                'VALIDATION',
                BackendStatus.DRAFT_MAILBOX_MISMATCH,
                'draft_reference 属于其他邮箱，已停止发送',
            )
        return _validation_result(
            identity,
            'NONE',
            BackendStatus.FALLBACK_REQUIRED,
            'Edge 草稿引用暂不支持安全发送；需要可定位且可校验的稳定 draft id',
        )

    if identity.mailbox_id != 'master_mail':
        return _validation_result(
            identity,
            'VALIDATION',
            BackendStatus.DRAFT_MAILBOX_MISMATCH,
            '当前邮箱不支持该 Graph draft reference',
        )

    result = _graph_backend().send_draft(MailSendRequest(
        draft_reference=draft_reference,
        confirmed=True,
    ))
    return _external_result(identity, 'GRAPH_API', result)


@mcp.tool()
def send_mail_draft(
    mailbox_id: str,
    draft_reference: str,
    confirm_send: bool = False,
) -> dict[str, Any]:
    """Send an existing draft only after validation and explicit confirmation."""
    identity = MAILBOX_IDENTITIES.get(mailbox_id)
    if identity is None:
        raise ValueError(f'Unknown mailbox identity: {mailbox_id}')

    reference = draft_reference.strip()
    if not reference:
        return _validation_result(
            identity,
            'VALIDATION',
            BackendStatus.INVALID_DRAFT,
            'draft_reference cannot be empty',
        )
    if confirm_send is not True:
        return _validation_result(
            identity,
            'VALIDATION',
            BackendStatus.NOT_CONFIRMED,
            '发送已有草稿需要 confirm_send=true 的明确确认',
        )
    if 'SEND' not in identity.permissions:
        return _validation_result(
            identity,
            'NONE',
            BackendStatus.FORBIDDEN,
            f'{identity.display_name}不允许发送邮件',
        )

    try:
        return _send_existing_draft(identity, reference)
    except Exception as error:
        LOGGER.exception('%s: send draft ERROR', identity.mailbox_id)
        return _external_result(
            identity,
            'GRAPH_API',
            MailSendResult(
                BackendStatus.REQUEST_FAILED,
                f'{type(error).__name__}: {error}',
            ),
        )


__all__ = ['send_mail_draft']
