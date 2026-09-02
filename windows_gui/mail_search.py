"""Unified READ-only mailbox search across configured backends."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any
import re

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
_MAX_NL_QUERY_CHARS = 300
_MAX_NL_KEYWORD_CHARS = 100
_NUMBER_WORDS = {'一': 1, '两': 2, '二': 2, '三': 3, '四': 4, '五': 5}
_RELATIVE_TIME_RE = re.compile(
    r'(?:最近|近|过去)\s*(\d+|[一二两三四五])?\s*'
    r'(天|日|周|星期|个?月)'
)


def parse_natural_mail_query(
    query: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Convert a small Chinese query into READ-only backend query fields."""
    original = ' '.join(str(query or '').split())
    if not original:
        raise ValueError('query cannot be empty')
    if len(original) > _MAX_NL_QUERY_CHARS:
        raise ValueError(f'query must be at most {_MAX_NL_QUERY_CHARS} characters')
    reference = now or datetime.now().astimezone()
    local_tz = reference.tzinfo
    start_time: datetime | None = None
    end_time: datetime | None = None
    working = original

    day_match = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', working)
    if day_match:
        try:
            start_day = datetime(
                int(day_match.group(1)), int(day_match.group(2)),
                int(day_match.group(3)), tzinfo=local_tz,
            )
        except ValueError as error:
            raise ValueError('query contains an invalid date') from error
        working = working.replace(day_match.group(0), '', 1)
        later = re.search(
            r'(?:到|至|to)\s*(\d{4})-(\d{1,2})-(\d{1,2})',
            working,
            re.IGNORECASE,
        )
        if later:
            try:
                end_day = datetime(
                    int(later.group(1)), int(later.group(2)),
                    int(later.group(3)), 23, 59, 59, tzinfo=local_tz,
                )
            except ValueError as error:
                raise ValueError('query contains an invalid date') from error
            working = working.replace(later.group(0), '', 1)
            end_time = end_day
        else:
            end_time = start_day + timedelta(days=1) - timedelta(seconds=1)
        start_time = start_day
    else:
        relative = _RELATIVE_TIME_RE.search(working)
        if relative:
            raw_number = relative.group(1) or '1'
            try:
                amount = int(raw_number)
            except ValueError:
                amount = _NUMBER_WORDS.get(raw_number, 1)
            unit = relative.group(2)
            amount = max(1, amount)
            if unit in ('周', '星期'):
                delta = timedelta(weeks=amount)
            elif '月' in unit:
                delta = timedelta(days=30 * amount)
            else:
                delta = timedelta(days=amount)
            start_time = reference - delta
            end_time = reference
            working = working.replace(relative.group(0), '', 1)
        elif re.search(r'今天|今日', working):
            start_time = reference.replace(hour=0, minute=0, second=0, microsecond=0)
            end_time = start_time + timedelta(days=1) - timedelta(seconds=1)
            working = re.sub(r'今天|今日', '', working, count=1)
        elif re.search(r'昨天|昨日', working):
            start_day = (reference - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            start_time = start_day
            end_time = start_day + timedelta(days=1) - timedelta(seconds=1)
            working = re.sub(r'昨天|昨日', '', working, count=1)

    specific = None
    for pattern in (
        r'(?:关于|有关|包含)\s*(.+?)\s*(?:的)?邮件$',
        r'(?:学校|大学|学院)(?:发送?的?|发来的?)\s*(.+?)\s*(?:的)?邮件$',
    ):
        match = re.search(pattern, working)
        if match:
            specific = match.group(1)
            working = ''
            break
    if specific is None:
        working = re.sub(
            r'^(?:请|帮我|帮忙)?(?:找|查找|搜索|搜一下|看看)',
            '',
            working,
        )
        working = re.sub(r'(?:的)?邮件$', '', working)
        working = re.sub(
            r'(?:学校|大学|学院)(?:发送?的?|发来的?)',
            '',
            working,
        )
        working = re.sub(r'关于|有关|包含|最近|近', '', working)
        specific = working.strip(' 的，。,.；;')

    keyword = str(specific or '').strip()
    sender = None
    if not keyword:
        if original != working and re.search(r'学校|大学|学院', original):
            sender = '学校'
        else:
            raise ValueError('query must contain a keyword or sender')
    if len(keyword) > _MAX_NL_KEYWORD_CHARS:
        raise ValueError(
            f'keyword must be at most {_MAX_NL_KEYWORD_CHARS} characters'
        )
    return {
        'keyword': keyword or None,
        'sender': sender,
        'start_time': start_time,
        'end_time': end_time,
    }


def natural_language_mail_search(
    query: str,
    *,
    max_results: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run existing READ-only search and return a concise safe summary."""
    if not 1 <= max_results <= _MAX_SEARCH_RESULTS:
        raise ValueError(
            f'max_results must be between 1 and {_MAX_SEARCH_RESULTS}'
        )
    parsed = parse_natural_mail_query(query, now=now)
    result = search_mailboxes(
        keyword=parsed['keyword'],
        sender=parsed['sender'],
        start_time=(
            parsed['start_time'].isoformat() if parsed['start_time'] else None
        ),
        end_time=parsed['end_time'].isoformat() if parsed['end_time'] else None,
        max_results=max_results,
    )
    results = [
        {
            'mailbox_id': email['mailbox_id'],
            'sender': email['sender'],
            'subject': email['subject'],
            'received_time': email['received_time'],
            'reference_kind': email['reference_kind'],
        }
        for group in result['mailboxes']
        for email in group.get('results', [])
    ]
    failed_mailboxes = [
        {'mailbox_id': group['mailbox_id'], 'status': group['status']}
        for group in result['mailboxes']
        if group.get('status') != 'READY'
    ]
    return {
        'query': {
            'text': ' '.join(str(query or '').split()),
            'keyword': parsed['keyword'],
            'sender': parsed['sender'],
            'start_time': (
                parsed['start_time'].isoformat() if parsed['start_time'] else None
            ),
            'end_time': (
                parsed['end_time'].isoformat() if parsed['end_time'] else None
            ),
        },
        'result_count': len(results),
        'results': results,
        'failed_mailboxes': failed_mailboxes,
        'read_state_change': 'NONE',
        'search_scope': 'READ_ONLY_METADATA',
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


__all__ = ['search_mailboxes', 'natural_language_mail_search']
