"""Unified READ-only mailbox search across configured backends."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

from .mail_backends import (
    BackendStatus,
    EdgeFallbackBackend,
    GraphBackendConfig,
    GraphReadonlyBackend,
    MailSearchEmail,
    MailSearchQuery,
    MailSearchResult,
    WindowsCredentialManagerTokenStore,
)
from .mail_summary import (
    _MAIL_CONTROL_TYPES,
    _ensure_mailbox_page,
    _parse_mail_accessible_name,
)
from .mailboxes import MAILBOX_IDENTITIES, MailboxIdentity
from .server import mcp


LOGGER = logging.getLogger(__name__)
_MAX_SEARCH_RESULTS = 50
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
}


def _parse_time_boundary(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError(
            'start_time and end_time must use ISO 8601 format'
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _parse_display_time(value: str) -> datetime | None:
    text = value.strip()
    local_tz = datetime.now().astimezone().tzinfo
    for pattern in ('%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M'):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=local_tz)
        except ValueError:
            continue
    try:
        if ':' in text and not any(character.isalpha() for character in text):
            clock = text.split()[0]
            hour, minute = (int(part) for part in clock.split(':')[:2])
            today = datetime.now().date()
            return datetime(
                today.year, today.month, today.day, hour, minute,
                tzinfo=local_tz,
            )
    except (ValueError, IndexError):
        return None
    return None


def _matches_query(
    parsed: dict[str, str],
    received: datetime | None,
    query: MailSearchQuery,
) -> bool:
    keyword = query.keyword.casefold() if query.keyword else None
    sender = query.sender.casefold() if query.sender else None
    if keyword and keyword not in (
        f"{parsed['subject']} {parsed['sender']}".casefold()
    ):
        return False
    if sender and sender not in parsed['sender'].casefold():
        return False
    if query.start_time is not None or query.end_time is not None:
        if received is None:
            return False
        if query.start_time is not None and received < query.start_time:
            return False
        if query.end_time is not None and received > query.end_time:
            return False
    return True


def _edge_reference(identity: MailboxIdentity, parsed: dict[str, str]) -> str:
    material = '|'.join((
        identity.mailbox_id, parsed['sender'], parsed['subject'], parsed['time']
    )).encode('utf-8')
    digest = hashlib.sha256(material).hexdigest()
    return f"edge:{identity.mailbox_id}:metadata:{digest}"


def _search_with_edge(
    identity: MailboxIdentity, query: MailSearchQuery
) -> MailSearchResult:
    snapshot, state = _ensure_mailbox_page(identity)
    if state != 'READY' or snapshot is None:
        return MailSearchResult(
            BackendStatus.FALLBACK_REQUIRED,
            f'Edge mailbox page is not ready: {state}',
        )

    results: list[MailSearchEmail] = []
    seen: set[tuple[str, str, str]] = set()
    for control in snapshot.get('controls', []):
        if control.get('control_type') not in _MAIL_CONTROL_TYPES:
            continue
        parsed = _parse_mail_accessible_name(str(control.get('name', '')))
        if parsed is None:
            continue
        key = (parsed['sender'], parsed['subject'], parsed['time'])
        if key in seen:
            continue
        seen.add(key)
        received = _parse_display_time(parsed['time'])
        if not _matches_query(parsed, received, query):
            continue
        results.append(MailSearchEmail(
            sender=parsed['sender'],
            subject=parsed['subject'],
            received_time=received or datetime.now().astimezone(),
            message_reference=_edge_reference(identity, parsed),
            reference_kind='EDGE_METADATA_HASH',
        ))
        if len(results) >= query.max_results:
            break
    return MailSearchResult(
        BackendStatus.READY,
        'Edge READ-only 搜索完成；仅覆盖当前已验证可见邮件列表元数据',
        tuple(results),
    )


def _graph_backend() -> GraphReadonlyBackend:
    config = GraphBackendConfig.from_environment()
    return GraphReadonlyBackend(
        config=config,
        token_store=WindowsCredentialManagerTokenStore(
            config.token_service, config.token_username
        ),
    )


def _search_mailbox(
    identity: MailboxIdentity, query: MailSearchQuery
) -> dict[str, Any]:
    backend_name = 'EDGE_GUI'
    scope = 'VISIBLE_LIST_METADATA'
    try:
        if identity.mailbox_id == 'master_mail':
            graph_result = _graph_backend().search(query)
            result = graph_result
            if result.status in _GRAPH_FALLBACK_STATUSES:
                LOGGER.warning(
                    '%s: Graph search unavailable (%s); '
                    'using read-only Edge fallback',
                    identity.mailbox_id,
                    result.status.value,
                )
                fallback = _search_with_edge(identity, query)
                result = MailSearchResult(
                    fallback.status,
                    (
                        f'Graph search unavailable ({graph_result.status.value}); '
                        f'Edge fallback: {fallback.message}'
                    ),
                    fallback.emails,
                    fallback.legacy_result,
                )
            else:
                backend_name = 'GRAPH_API'
                scope = 'SERVER_METADATA'
        else:
            result = EdgeFallbackBackend(
                summarize=lambda: {},
                search_messages=lambda search_query: _search_with_edge(
                    identity, search_query
                ),
            ).search(query)


        return {
            'mailbox_id': identity.mailbox_id,
            'display_name': identity.display_name,
            'backend': backend_name,
            'search_scope': scope,
            'status': _STATUS_MAP[result.status],
            'message': result.message,
            'result_count': len(result.emails),
            'results': [
                email.as_result(identity.mailbox_id)
                for email in result.emails
            ],
            'read_state_change': 'NONE',
        }
    except Exception as error:
        LOGGER.exception('%s: search ERROR', identity.mailbox_id)
        return {
            'mailbox_id': identity.mailbox_id,
            'display_name': identity.display_name,
            'backend': backend_name,
            'search_scope': scope,
            'status': 'ERROR',
            'message': f'{type(error).__name__}: {error}',
            'result_count': 0,
            'results': [],
            'read_state_change': 'NONE',
        }


@mcp.tool()
def search_mailboxes(
    mailbox_id: str | None = None,
    keyword: str | None = None,
    sender: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Search configured mailboxes without opening bodies or changing state."""
    selected = list(MAILBOX_IDENTITIES.values())
    if mailbox_id is not None:
        if mailbox_id not in MAILBOX_IDENTITIES:
            raise ValueError(f'Unknown mailbox identity: {mailbox_id}')
        selected = [MAILBOX_IDENTITIES[mailbox_id]]
    if not 1 <= max_results <= _MAX_SEARCH_RESULTS:
        raise ValueError(
            f'max_results must be between 1 and {_MAX_SEARCH_RESULTS}'
        )

    normalized_keyword = keyword.strip() if keyword else None
    normalized_sender = sender.strip() if sender else None
    parsed_start = _parse_time_boundary(start_time) if start_time else None
    parsed_end = _parse_time_boundary(end_time) if end_time else None
    if (
        parsed_start is not None
        and parsed_end is not None
        and parsed_start > parsed_end
    ):
        raise ValueError('start_time must not be later than end_time')

    query = MailSearchQuery(
        keyword=normalized_keyword,
        sender=normalized_sender,
        start_time=parsed_start,
        end_time=parsed_end,
        max_results=max_results,
    )
    groups = [_search_mailbox(identity, query) for identity in selected]
    all_results = [
        email for group in groups for email in group['results']
    ]
    all_results.sort(
        key=lambda email: datetime.fromisoformat(email['received_time']),
        reverse=True,
    )
    all_results = all_results[:max_results]
    kept_references = {
        email['message_reference'] for email in all_results
    }
    for group in groups:
        group['results'] = [
            email for email in group['results']
            if email['message_reference'] in kept_references
        ]
        group['result_count'] = len(group['results'])
    return {
        'query': {
            'mailbox_id': mailbox_id,
            'keyword': normalized_keyword,
            'sender': normalized_sender,
            'start_time': parsed_start.isoformat() if parsed_start else None,
            'end_time': parsed_end.isoformat() if parsed_end else None,
            'max_results': max_results,
        },
        'result_count': len(all_results),
        'mailboxes': groups,
        'read_state_change': 'NONE',
    }


__all__ = ['search_mailboxes']
