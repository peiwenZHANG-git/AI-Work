"""Nightly read-only mail digest with a Windows toast notification.

Combines the read-only IMAP backends for QQ and bachelor mailboxes with the
delegated Graph read-only backend for the Paris-Saclay Outlook mailbox, then
writes a local HTML digest file and shows a desktop toast. Message bodies are
fetched read-only and summarized in Chinese with the GLM API when a key is
configured; mailbox read state is never changed.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import imaplib
import json
import logging
import os
import re
import ssl
import subprocess
import time
import winreg
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import keyring
import requests

from windows_gui.imap_mail import (
    BACHELOR_IMAP_CREDENTIAL_SERVICE,
    BACHELOR_IMAP_CREDENTIAL_USERNAME,
    BACHELOR_IMAP_HOST,
    BACHELOR_IMAP_PORT,
    BachelorImapConfig,
    QQ_IMAP_CREDENTIAL_SERVICE,
    QQ_IMAP_CREDENTIAL_USERNAME,
    QQ_IMAP_HOST,
    QQ_IMAP_PORT,
    QqImapConfig,
    _default_imap_factory,
    fetch_messages_readonly,
)
from windows_gui.mail_backends import (
    BackendStatus,
    GraphBackendConfig,
    WindowsCredentialManagerSecretStore,
)
import sys


CREDENTIAL_SERVICE = 'AI-Work/windows-gui/mailboxes'
MASTER_REFRESH_USERNAME = 'master_mail_graph_refresh_token'
MASTER_REFRESH_TARGET = f'{MASTER_REFRESH_USERNAME}@{CREDENTIAL_SERVICE}'
MASTER_GRAPH_SCOPE = 'https://graph.microsoft.com/Mail.Read offline_access'
CRED_TYPE_GENERIC = 1
CRED_PERSIST_ENTERPRISE = 2
CRED_MAX_BLOB_BYTES = 2560
ERROR_NOT_FOUND = 1168
MAX_EMAILS_PER_MAILBOX = 25
MAX_FETCH_PER_MAILBOX = 50
MAX_IMAP_MESSAGE_BYTES = 400_000
MAX_BODY_CHARS = 3500
MAX_AI_MAILS_PER_RUN = 25
GRAPH_MESSAGES_URL = 'https://graph.microsoft.com/v1.0/me/messages'
ZHIPU_CHAT_ENDPOINT = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
DEFAULT_SUMMARY_MODEL = 'glm-4-flash'
SUMMARY_API_KEY_USERNAME = 'zhipu_glm_api_key'
SUMMARY_SYSTEM_PROMPT = (
    '你是邮件整理助手。用户会提供一封邮件，请只输出一个 JSON 对象，'
    '格式为 {"summary": "...", "importance": "高|中|低"}。'
    'summary 是一到两句话的简体中文要点，保留关键日期、地点或行动项；'
    'importance 是这封邮件对收件人的重要程度：需要行动、有截止日期、'
    '与本人直接相关为"高"；常规通知为"中"；广告营销为"低"。'
    '不要输出 JSON 以外的任何文字。'
)
TRANSLATION_SYSTEM_PROMPT = (
    '你是专业翻译。把用户提供的邮件正文完整翻译成简体中文，'
    '保留段落。只输出译文，不要任何解释。'
)
_QUOTED_MARKERS = ('-----', 'De :', 'De:', 'From:', '发件人:', 'Sent from', '原文')
IMPORTANCE_RANK = {'高': 0, '中': 1, '低': 2}
VALID_IMPORTANCE = ('高', '中', '低')
TOAST_APP_ID = (
    r'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}'
    r'\WindowsPowerShell\v1.0\powershell.exe'
)
DIGEST_DIR = (
    Path(os.environ.get('LOCALAPPDATA') or Path.home())
    / 'AI-Work'
    / 'mail-digests'
)


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ('dwLowDateTime', wt.DWORD),
        ('dwHighDateTime', wt.DWORD),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ('Flags', wt.DWORD),
        ('Type', wt.DWORD),
        ('TargetName', wt.LPWSTR),
        ('Comment', wt.LPWSTR),
        ('LastWritten', _FILETIME),
        ('CredentialBlobSize', wt.DWORD),
        ('CredentialBlob', ctypes.c_void_p),
        ('Persist', wt.DWORD),
        ('AttributeCount', wt.DWORD),
        ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wt.LPWSTR),
        ('UserName', wt.LPWSTR),
    ]

_USER_ENV_KEYS = (
    'AI_WORK_QQ_IMAP_USERNAME',
    'AI_WORK_BACHELOR_IMAP_USERNAME',
    'AI_WORK_OUTLOOK_TENANT_ID',
    'AI_WORK_OUTLOOK_CLIENT_ID',
    'AI_WORK_OUTLOOK_MAILBOX',
)

_LOGGER = logging.getLogger(__name__)


class MailboxFlowError(Exception):
    """Raised when the master mailbox delegated-login flow cannot proceed."""


OK_STATUSES = frozenset({
    BackendStatus.READY.value,
    BackendStatus.EMPTY_TODAY.value,
})


class SummaryAPIError(Exception):
    """Raised when the GLM summary API call cannot produce a summary."""


@dataclass
class DigestMail:
    sender: str
    subject: str
    time: str
    body_text: str = ''
    summary: str = ''
    translation: str = ''
    importance: str = '中'


@dataclass
class MailboxDigest:
    mailbox_id: str
    display_name: str
    short_name: str
    status: str
    message: str
    emails: list[Any] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES


def ensure_environment() -> None:
    """Copy missing user-scope variables from the registry into the process.

    Task Scheduler starts processes with the persisted user environment, but
    interactive sessions started before the variables were created do not see
    them, so the digest can run correctly from either context.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            for name in _USER_ENV_KEYS:
                if os.environ.get(name, '').strip():
                    continue
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if isinstance(value, str) and value.strip():
                    os.environ[name] = value
    except OSError:
        _LOGGER.debug('registry environment unavailable', exc_info=True)


def read_master_refresh_token() -> str:
    """Read the stored Paris-Saclay refresh token (UTF-8 blob)."""
    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
    cred_ptr = ctypes.c_void_p()
    if not advapi32.CredReadW(
        MASTER_REFRESH_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)
    ):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            raise MailboxFlowError('未找到登录授权；请重新完成一次性登录')
        raise ctypes.WinError(error)
    try:
        credential = ctypes.cast(
            cred_ptr, ctypes.POINTER(_CREDENTIALW)
        ).contents
        blob = ctypes.string_at(
            credential.CredentialBlob, credential.CredentialBlobSize
        )
    finally:
        advapi32.CredFree(cred_ptr)
    return blob.decode('utf-8')


def write_master_refresh_token(token: str) -> None:
    """Persist the latest rotated refresh token for the next nightly run."""
    blob = token.encode('utf-8')
    if len(blob) > CRED_MAX_BLOB_BYTES:
        raise ValueError('refresh token exceeds the credential blob limit')
    buffer = ctypes.create_string_buffer(blob)
    credential = _CREDENTIALW()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = MASTER_REFRESH_TARGET
    credential.Comment = 'AI-Work master_mail graph refresh token'
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.c_void_p)
    credential.Persist = CRED_PERSIST_ENTERPRISE
    credential.UserName = MASTER_REFRESH_USERNAME
    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def exchange_master_refresh_token(refresh_token: str) -> dict[str, Any]:
    """Exchange the stored refresh token for a fresh in-memory access token."""
    tenant = os.environ.get('AI_WORK_OUTLOOK_TENANT_ID', '').strip()
    client = os.environ.get('AI_WORK_OUTLOOK_CLIENT_ID', '').strip()
    if not tenant or not client:
        raise MailboxFlowError('Graph 租户或应用 ID 环境变量未配置')
    base = f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0'
    try:
        response = requests.post(
            f'{base}/token',
            data={
                'grant_type': 'refresh_token',
                'client_id': client,
                'refresh_token': refresh_token,
                'scope': MASTER_GRAPH_SCOPE,
            },
            timeout=15,
        )
    except requests.RequestException as error:
        raise MailboxFlowError(f'Graph 令牌刷新网络失败：{type(error).__name__}')
    payload = response.json()
    if 'access_token' not in payload:
        raise MailboxFlowError(
            f"Graph 令牌刷新失败：{payload.get('error', 'unknown_error')}"
        )
    return payload


def strip_html(text: str) -> str:
    text = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', text)
    text = re.sub(r'(?s)<[^>]+>', ' ', text)
    text = unescape(text)
    return ' '.join(text.split())


def clean_body_text(text: str, limit: int = MAX_BODY_CHARS) -> str:
    text = ' '.join(str(text or '').split())
    if not text:
        return ''
    cut = len(text)
    for marker in _QUOTED_MARKERS:
        position = text.find(marker, 1)
        if 0 < position < cut:
            cut = position
    return text[:cut].strip()[:limit]


def extract_body_text(message: Any) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_maintype() != 'text':
            continue
        if part.get_content_disposition() == 'attachment':
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or 'utf-8'
        try:
            text = payload.decode(charset, errors='replace')
        except LookupError:
            text = payload.decode('utf-8', errors='replace')
        if part.get_content_subtype() == 'plain':
            plain_parts.append(text)
        else:
            html_parts.append(text)
    if plain_parts:
        return clean_body_text(' '.join(plain_parts))
    if html_parts:
        return clean_body_text(strip_html(' '.join(html_parts)))
    return ''


def graph_body_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ''
    content = str(body.get('content') or '')
    if not content:
        return ''
    if str(body.get('contentType') or '').casefold() == 'html':
        return clean_body_text(strip_html(content))
    return clean_body_text(content)


def build_full_ai_prompt(mail: DigestMail) -> str:
    return (
        f'主题：{mail.subject}\n'
        f'发件人：{mail.sender}\n'
        f'时间：{mail.time}\n'
        f'正文：\n{mail.body_text}\n\n'
        '请只输出一个 JSON 对象，格式为 '
        '{"summary": "...", "importance": "高|中|低"}，'
        '用简体中文概括这封邮件的要点，'
        '不要包含 JSON 以外的任何文字。'
    )


def load_summary_api_key() -> str:
    try:
        secret = keyring.get_password(CREDENTIAL_SERVICE, SUMMARY_API_KEY_USERNAME)
    except Exception:
        return ''
    return secret if isinstance(secret, str) else ''


def parse_ai_json(content: str) -> dict[str, Any] | None:
    text = str(content or '').strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _chat_once(
    post: Any,
    api_key: str,
    model_name: str,
    system_prompt: str,
    user_content: str,
    timeout: int = 45,
) -> str:
    last_error: SummaryAPIError | None = None
    for attempt in range(2):
        if attempt:
            time.sleep(2)
        try:
            response = post(
                ZHIPU_CHAT_ENDPOINT,
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                },
                json={
                    'model': model_name,
                    'messages': [
                        {'role': 'system', 'content': system_prompt},
                        {'role': 'user', 'content': user_content},
                    ],
                    'temperature': 0.2,
                    'max_tokens': 2000,
                },
                timeout=timeout,
            )
        except requests.RequestException as error:
            last_error = SummaryAPIError(
                f'摘要接口网络失败：{type(error).__name__}'
            )
            continue
        try:
            payload = response.json()
        except ValueError as error:
            raise SummaryAPIError('摘要接口响应不是 JSON') from error
        if response.status_code != 200 or 'choices' not in payload:
            detail = ''
            if isinstance(payload, dict):
                error_info = payload.get('error')
                if isinstance(error_info, dict):
                    detail = str(error_info.get('message') or '')
            raise SummaryAPIError(
                f'摘要接口失败：HTTP {response.status_code} {detail}'
            )
        try:
            return str(payload['choices'][0]['message']['content'])
        except (KeyError, IndexError, TypeError) as error:
            raise SummaryAPIError('摘要接口响应缺少内容') from error
    raise last_error or SummaryAPIError('摘要接口请求失败')
    try:
        payload = response.json()
    except ValueError as error:
        raise SummaryAPIError('摘要接口响应不是 JSON') from error
    if response.status_code != 200 or 'choices' not in payload:
        detail = ''
        if isinstance(payload, dict):
            error_info = payload.get('error')
            if isinstance(error_info, dict):
                detail = str(error_info.get('message') or '')
        raise SummaryAPIError(
            f'摘要接口失败：HTTP {response.status_code} {detail}'
        )
    try:
        return str(payload['choices'][0]['message']['content'])
    except (KeyError, IndexError, TypeError) as error:
        raise SummaryAPIError('摘要接口响应缺少内容') from error


def _resolve_model(model: str | None) -> str:
    return (
        model
        or os.environ.get('AI_WORK_SUMMARY_MODEL', '').strip()
        or DEFAULT_SUMMARY_MODEL
    )


def call_mail_summary(
    mail: DigestMail,
    api_key: str,
    *,
    model: str | None = None,
    transport: Any = None,
) -> str:
    model_name = _resolve_model(model)
    post = transport or requests.post
    content = _chat_once(
        post,
        api_key,
        model_name,
        SUMMARY_SYSTEM_PROMPT,
        build_full_ai_prompt(mail),
    )
    parsed = parse_ai_json(content)
    if parsed is None:
        raise SummaryAPIError('AI 响应不是有效的 JSON')
    summary = ' '.join(str(parsed.get('summary') or '').split())
    if not summary:
        raise SummaryAPIError('AI 未返回摘要')
    importance = str(parsed.get('importance') or '').strip()
    if importance not in VALID_IMPORTANCE:
        importance = '中'
    return summary, importance


def call_mail_translation(
    body_text: str,
    api_key: str,
    *,
    model: str | None = None,
    transport: Any = None,
) -> str:
    model_name = _resolve_model(model)
    post = transport or requests.post
    content = _chat_once(
        post,
        api_key,
        model_name,
        TRANSLATION_SYSTEM_PROMPT,
        body_text,
        timeout=90,
    )
    translation = content.strip()
    if not _contains_cjk(translation):
        retry_user = (
            body_text
            + '\n\n（上一次输出不是简体中文，这是错误的。'
            '请把上面的邮件正文完整翻译成简体中文，只输出译文。）'
        )
        content = _chat_once(
            post,
            api_key,
            model_name,
            TRANSLATION_SYSTEM_PROMPT,
            retry_user,
            timeout=90,
        )
        translation = content.strip()
        if not _contains_cjk(translation):
            raise SummaryAPIError('AI 未返回中文翻译')
    return translation


def _contains_cjk(text: str) -> bool:
    return any('\u4e00' <= ch <= '\u9fff' for ch in str(text or ''))


def _mail_cache_key(mailbox_id: str, mail: Any) -> str:
    raw = f'{mailbox_id}|{mail.subject}|{getattr(mail, "body_text", "")}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:40]


def load_translation_cache() -> dict[str, Any]:
    try:
        loaded = json.loads(
            (DIGEST_DIR / 'translation-cache.json').read_text(encoding='utf-8')
        )
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_translation_cache(cache: dict[str, Any]) -> None:
    try:
        DIGEST_DIR.mkdir(parents=True, exist_ok=True)
        (DIGEST_DIR / 'translation-cache.json').write_text(
            json.dumps(cache, ensure_ascii=False),
            encoding='utf-8',
        )
    except OSError:
        _LOGGER.warning('could not persist translation cache')


RUN_LOCK_PATH = DIGEST_DIR / 'run.lock'
LOCK_STALE_SECONDS = 900


def _acquire_run_lock() -> bool:
    """Best-effort single-instance lock; stale locks (>15 min) are reclaimed."""
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(str(RUN_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(handle, str(os.getpid()).encode('utf-8'))
        os.close(handle)
        return True
    except FileExistsError:
        try:
            age = time.time() - os.stat(RUN_LOCK_PATH).st_mtime
        except OSError:
            return False
        if age <= LOCK_STALE_SECONDS:
            return False
        try:
            os.remove(RUN_LOCK_PATH)
        except OSError:
            return False
        return _acquire_run_lock()


def _release_run_lock() -> None:
    try:
        os.remove(RUN_LOCK_PATH)
    except OSError:
        pass


def enrich_digests(
    mailboxes: list[MailboxDigest],
    api_key: str,
    transport: Any = None,
    cache: dict[str, Any] | None = None,
) -> None:
    persistent = cache is None
    cache = load_translation_cache() if cache is None else cache
    remaining = MAX_AI_MAILS_PER_RUN
    active_key = api_key
    consecutive_failures = 0
    for box in mailboxes:
        if not box.ok:
            continue
        for mail in box.emails:
            body_text = getattr(mail, 'body_text', '')
            if not body_text:
                mail.summary = ''
                mail.translation = ''
                continue
            cache_key = _mail_cache_key(box.mailbox_id, mail)
            cached = cache.get(cache_key)
            cached_valid = (
                isinstance(cached, dict)
                and 'summary' in cached
                and _contains_cjk(str(cached.get('translation') or ''))
            )
            if cached_valid:
                mail.summary = str(cached.get('summary') or '')
                mail.translation = str(cached.get('translation') or '')
                cached_importance = str(cached.get('importance') or '中')
                mail.importance = (
                    cached_importance
                    if cached_importance in VALID_IMPORTANCE
                    else '中'
                )
                continue
            if not (active_key and remaining > 0):
                mail.summary = '正文预览：' + _single_line(body_text, 140)
                mail.translation = ''
                mail.importance = '中'
                continue
            try:
                mail.summary, mail.importance = call_mail_summary(
                    mail, active_key, transport=transport
                )
                consecutive_failures = 0
            except SummaryAPIError as error:
                _LOGGER.warning('AI 摘要失败：%s', error)
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    active_key = ''
                mail.summary = '正文预览：' + _single_line(body_text, 140)
                mail.translation = ''
                mail.importance = '中'
                continue
            if not active_key:
                mail.summary = '正文预览：' + _single_line(body_text, 140)
                mail.translation = ''
                mail.importance = '中'
                continue
            try:
                mail.translation = call_mail_translation(
                    body_text, active_key, transport=transport
                )
                consecutive_failures = 0
            except SummaryAPIError as error:
                _LOGGER.warning('AI 翻译失败：%s', error)
                mail.translation = ''
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    active_key = ''
            if _contains_cjk(mail.translation):
                cache[cache_key] = {
                    'summary': mail.summary,
                    'translation': mail.translation,
                    'importance': mail.importance,
                }
                if persistent:
                    save_translation_cache(cache)
            remaining -= 1
    for box in mailboxes:
        _sort_box_mails(box)
    if persistent:
        save_translation_cache(cache)


def _imap_message_to_mail(item: Any) -> DigestMail:
    message = item.message
    received = item.received
    if received is not None:
        time_text = received.isoformat()
    else:
        date_header = str(message.get('Date') or '')
        try:
            time_text = parsedate_to_datetime(date_header).isoformat()
        except (TypeError, ValueError):
            time_text = date_header
    display_name, address = parseaddr(str(message.get('From') or ''))
    sender = display_name or address or str(message.get('From') or '')
    return DigestMail(
        sender=sender,
        subject=str(message.get('Subject') or ''),
        time=time_text,
        body_text=extract_body_text(message),
    )


def _collect_imap_digest(
    mailbox_id: str,
    display_name: str,
    short_name: str,
    config: Any,
    credential_service: str,
    credential_username: str,
    host: str,
    port: int,
) -> MailboxDigest:
    username = config.username
    password = WindowsCredentialManagerSecretStore(
        credential_service, credential_username
    ).get_secret()
    if not username or not password:
        return MailboxDigest(
            mailbox_id=mailbox_id,
            display_name=display_name,
            short_name=short_name,
            status=BackendStatus.IMAP_NOT_CONFIGURED.value,
            message='用户名或授权码未配置',
        )
    try:
        messages = fetch_messages_readonly(
            _default_imap_factory,
            host,
            port,
            username,
            password,
            since_date=datetime.now().astimezone().date() - timedelta(days=1),
            limit=MAX_FETCH_PER_MAILBOX,
            max_message_bytes=MAX_IMAP_MESSAGE_BYTES,
        )
    except imaplib.IMAP4.error as error:
        status = (
            BackendStatus.IMAP_AUTH_FAILED.value
            if 'auth' in str(error).casefold()
            else BackendStatus.IMAP_PROTOCOL_ERROR.value
        )
        return MailboxDigest(
            mailbox_id=mailbox_id,
            display_name=display_name,
            short_name=short_name,
            status=status,
            message=f'IMAP 读取失败：{error}',
        )
    except (OSError, ssl.SSLError) as error:
        return MailboxDigest(
            mailbox_id=mailbox_id,
            display_name=display_name,
            short_name=short_name,
            status=BackendStatus.REQUEST_FAILED.value,
            message=f'IMAP 网络失败：{type(error).__name__}',
        )
    except Exception as error:  # never let one mailbox kill the whole digest
        return MailboxDigest(
            mailbox_id=mailbox_id,
            display_name=display_name,
            short_name=short_name,
            status=BackendStatus.REQUEST_FAILED.value,
            message=f'IMAP 读取异常：{type(error).__name__}',
        )
    if not messages:
        return MailboxDigest(
            mailbox_id=mailbox_id,
            display_name=display_name,
            short_name=short_name,
            status=BackendStatus.EMPTY_TODAY.value,
            message='过去 24 小时没有新邮件',
        )
    window_start = datetime.now().astimezone() - timedelta(hours=24)
    mails = []
    for item in messages:
        mail = _imap_message_to_mail(item)
        parsed = _mail_dt(mail.time)
        if parsed is not None and parsed < window_start:
            continue
        mails.append(mail)
    if not mails:
        return MailboxDigest(
            mailbox_id=mailbox_id,
            display_name=display_name,
            short_name=short_name,
            status=BackendStatus.EMPTY_TODAY.value,
            message='过去 24 小时没有新邮件',
        )
    return MailboxDigest(
        mailbox_id=mailbox_id,
        display_name=display_name,
        short_name=short_name,
        status=BackendStatus.READY.value,
        message='IMAP 只读检查完成（覆盖过去 24 小时）；正文仅用于摘要，未改变已读状态',
        emails=mails,
    )


def collect_qq_digest() -> MailboxDigest:
    return _collect_imap_digest(
        'qq_mail',
        'QQ 邮箱',
        'QQ',
        QqImapConfig.from_environment(),
        QQ_IMAP_CREDENTIAL_SERVICE,
        QQ_IMAP_CREDENTIAL_USERNAME,
        QQ_IMAP_HOST,
        QQ_IMAP_PORT,
    )


def collect_bachelor_digest() -> MailboxDigest:
    return _collect_imap_digest(
        'bachelor_mail',
        '传媒大学本科邮箱',
        '本科',
        BachelorImapConfig.from_environment(),
        BACHELOR_IMAP_CREDENTIAL_SERVICE,
        BACHELOR_IMAP_CREDENTIAL_USERNAME,
        BACHELOR_IMAP_HOST,
        BACHELOR_IMAP_PORT,
    )


def collect_master_digest() -> MailboxDigest:
    window_start = datetime.now().astimezone() - timedelta(hours=24)

    def failed(status: BackendStatus, message: str) -> MailboxDigest:
        return MailboxDigest(
            mailbox_id='master_mail',
            display_name='巴黎萨克雷邮箱',
            short_name='萨克雷',
            status=status.value,
            message=message,
        )

    try:
        refresh_token = read_master_refresh_token()
    except MailboxFlowError as error:
        return failed(BackendStatus.NOT_AUTHENTICATED, str(error))
    try:
        payload = exchange_master_refresh_token(refresh_token)
    except MailboxFlowError as error:
        status = (
            BackendStatus.TOKEN_EXPIRED.value
            if 'invalid_grant' in str(error)
            else BackendStatus.REQUEST_FAILED.value
        )
        return failed(status, str(error))
    if payload.get('refresh_token'):
        try:
            write_master_refresh_token(payload['refresh_token'])
        except (OSError, ValueError):
            _LOGGER.warning('could not persist rotated refresh token')

    config = GraphBackendConfig.from_environment()
    if not config.is_configured:
        return failed(BackendStatus.NOT_AUTHENTICATED, 'Graph 环境变量不完整')

    params = urlencode({
        '$top': MAX_FETCH_PER_MAILBOX,
        '$select': 'subject,sender,receivedDateTime,body',
        '$orderby': 'receivedDateTime desc',
    })
    response = None
    for attempt in range(2):
        if attempt:
            time.sleep(2)
        try:
            response = requests.get(
                f'{GRAPH_MESSAGES_URL}?{params}',
                headers={'Authorization': f'Bearer {payload["access_token"]}'},
                timeout=30,
            )
        except requests.RequestException as error:
            if attempt:
                return failed(
                    BackendStatus.REQUEST_FAILED,
                    f'Graph 请求失败：{type(error).__name__}',
                )
            continue
        break
    if response.status_code == 401:
        return failed(BackendStatus.TOKEN_EXPIRED, 'Graph 访问令牌已失效')
    if response.status_code != 200:
        return failed(
            BackendStatus.REQUEST_FAILED,
            f'Graph 请求失败：HTTP {response.status_code}',
        )
    try:
        value = response.json().get('value', [])
    except ValueError:
        return failed(
            BackendStatus.REQUEST_FAILED,
            'Graph 响应不是有效的 JSON 对象',
        )

    mails: list[DigestMail] = []
    for item in value:
        received_raw = str(item.get('receivedDateTime') or '')
        try:
            received_local = datetime.fromisoformat(
                received_raw.replace('Z', '+00:00')
            ).astimezone()
        except ValueError:
            received_local = None
        if received_local is None or received_local < window_start:
            continue
        sender_info = (item.get('sender') or {}).get('emailAddress') or {}
        mails.append(
            DigestMail(
                sender=str(sender_info.get('name') or sender_info.get('address') or ''),
                subject=str(item.get('subject') or ''),
                time=received_raw,
                body_text=graph_body_text(item.get('body')),
            )
        )

    return MailboxDigest(
        mailbox_id='master_mail',
        display_name='巴黎萨克雷邮箱',
        short_name='萨克雷',
        status=BackendStatus.READY.value,
        message='Graph 只读检查完成（覆盖过去 24 小时）；正文仅用于摘要，未改变已读状态',
        emails=mails,
    )


def _single_line(text: Any, limit: int) -> str:
    collapsed = ' '.join(str(text or '').split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + '…'


def _mail_time(value: Any) -> str:
    parsed = _mail_dt(value)
    if parsed is not None:
        return parsed.strftime('%H:%M')
    return str(value or '')[:16]


def _mail_dt(value: Any) -> datetime | None:
    raw = str(value or '')
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone()
    except ValueError:
        return None


def _within_24h(mail: Any, now: datetime) -> bool:
    parsed = _mail_dt(getattr(mail, 'time', ''))
    if parsed is None:
        return True
    return parsed >= now - timedelta(hours=24)


def _mail_sort_key(mail: Any):
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    parsed = _mail_dt(getattr(mail, 'time', ''))
    timestamp = parsed.timestamp() if parsed else epoch.timestamp()
    rank = IMPORTANCE_RANK.get(getattr(mail, 'importance', '中'), 1)
    return (rank, -timestamp)


def _sort_box_mails(box: MailboxDigest) -> None:
    box.emails.sort(key=_mail_sort_key)


def _render_mail_list_item(mail: Any) -> str:
    summary = getattr(mail, 'summary', '')
    translation = getattr(mail, 'translation', '')
    body_text = getattr(mail, 'body_text', '')
    importance = getattr(mail, 'importance', '中')
    imp_class = {'高': 'imp-high', '中': 'imp-mid', '低': 'imp-low'}.get(
        importance, 'imp-mid'
    )
    summary_html = (
        f'<p class="mail-summary"><span class="label">摘要</span>'
        f'{escape(_single_line(summary, 200))}</p>'
        if summary
        else ''
    )
    translation_html = (
        f'<h3 class="detail-title">全文翻译</h3>'
        f'<div class="translation">{escape(translation)}</div>'
        if translation
        else ''
    )
    original_html = (
        f'<details class="original"><summary>查看原文</summary>'
        f'<div class="original-body">{escape(body_text)}</div></details>'
        if body_text
        else ''
    )
    return (
        '<article class="mail">'
        '<button class="mail-head" type="button">'
        f'<span class="time">{escape(_mail_time(mail.time))}</span>'
        f'<span class="sender">{escape(_single_line(mail.sender, 60))}</span>'
        f'<span class="subject"><span class="imp {imp_class}">'
        f'{escape(importance)}</span>{escape(str(mail.subject or ""))}</span>'
        '<svg class="chevron" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
        '<path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.6" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        '</button>'
        f'{summary_html}'
        f'<div class="mail-detail">{translation_html}{original_html}</div>'
        '</article>'
    )


def format_digest(mailboxes: list[MailboxDigest], generated_at: datetime) -> str:
    now = (
        generated_at
        if generated_at.tzinfo
        else generated_at.astimezone()
    )
    lines = [
        f'每日邮件摘要 · {generated_at:%Y-%m-%d}',
        f'生成时间：{generated_at:%H:%M:%S}',
        '',
    ]
    failed_boxes = [box for box in mailboxes if not box.ok]
    ok_boxes = [box for box in mailboxes if box.ok]
    if failed_boxes:
        lines.append('⚠ 读取失败的邮箱')
        for box in failed_boxes:
            lines.append(
                f'【{box.display_name}】读取失败（{box.status}）：'
                f'{_single_line(box.message, 120)}'
            )
        lines.append('')
    if not ok_boxes:
        lines.append('')
    for box in ok_boxes:
        mails = sorted(box.emails, key=_mail_sort_key)
        lines.append(f'【{box.display_name}】')
        if not mails:
            lines.append('  过去 24 小时没有新邮件')
            continue
        lines.append(f'  {len(mails)} 封')
        for index, mail in enumerate(mails, 1):
            importance = getattr(mail, 'importance', '中')
            lines.append(
                f'  {index}. [{importance}] {_mail_time(mail.time)} '
                f'{_single_line(mail.sender, 40)} — {_single_line(mail.subject, 70)}'
            )
            summary = getattr(mail, 'summary', '')
            if summary:
                lines.append(f'     ↳ {_single_line(summary, 110)}')
        lines.append('')
    lines.append('只读模式：正文仅用于生成摘要，未改变已读状态')
    return '\n'.join(lines)


HTML_STYLES = """
* { box-sizing: border-box; }
body { margin: 0; font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; background: #eef1f5; color: #1f2430; }
header { background: #1f2933; color: #ffffff; padding: 26px 24px 22px; }
header .inner { max-width: 860px; margin: 0 auto; }
header h1 { margin: 0 0 10px; font-size: 22px; letter-spacing: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip { font-size: 12px; padding: 3px 10px; border-radius: 4px; background: rgba(255,255,255,0.14); color: #e5e9ee; }
.chip.ok { background: #1f7a45; }
.chip.warn { background: #b3261e; }
main { max-width: 860px; margin: 22px auto 40px; padding: 0 16px; }
section.mailbox { background: #ffffff; border: 1px solid #dfe3e8; border-radius: 8px; margin-bottom: 18px; overflow: hidden; }
.mailbox-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 14px 18px; border-bottom: 1px solid #eef1f4; }
.mailbox-head h2 { margin: 0; font-size: 16px; }
.head-meta { display: flex; align-items: center; gap: 8px; }
.count-badge { font-size: 12px; color: #52606d; background: #f1f4f7; border: 1px solid #e0e4e8; padding: 2px 10px; border-radius: 4px; }
.head-actions { display: flex; gap: 6px; }
.head-actions button { font: inherit; font-size: 12px; color: #334e68; background: #f6f8fa; border: 1px solid #dfe3e8; border-radius: 4px; padding: 3px 10px; cursor: pointer; }
.head-actions button:hover { background: #eef2f6; }
.mail-list { list-style: none; margin: 0; padding: 0; }
article.mail + article.mail { border-top: 1px solid #eef1f4; }
.mail-head { display: grid; grid-template-columns: 46px minmax(110px, 200px) 1fr 18px; gap: 10px; align-items: start; width: 100%; padding: 10px 14px; background: transparent; border: 0; font: inherit; text-align: left; cursor: pointer; }
.mail-head:hover { background: #f6f8fa; }
.mail .time { color: #7b8794; font-size: 13px; font-variant-numeric: tabular-nums; padding-top: 1px; }
.mail .sender { color: #486581; font-size: 13px; overflow-wrap: anywhere; padding-top: 1px; }
.mail .subject { color: #1f2430; font-size: 14px; overflow-wrap: anywhere; }
.chevron { width: 14px; height: 14px; margin-top: 3px; color: #829ab1; transition: transform 0.15s ease; }
.mail.open .chevron { transform: rotate(180deg); }
.mail-detail { display: none; padding: 4px 16px 16px 70px; background: #fbfcfd; border-top: 1px solid #eef1f4; }
.mail.open .mail-detail { display: block; }
.mail-summary { margin: 6px 14px 10px 70px; font-size: 13px; color: #243b53; background: #f0f6ff; border: 1px solid #d6e4ff; border-radius: 6px; padding: 6px 10px; }
.mail-summary .label { color: #2563eb; font-weight: 600; margin-right: 6px; }
.detail-title { margin: 0 0 6px; font-size: 13px; color: #52606d; }
.translation { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.7; color: #243b53; }
details.original { margin-top: 12px; }
details.original summary { cursor: pointer; font-size: 13px; color: #829ab1; }
.original-body { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; line-height: 1.6; color: #52606d; background: #f6f8fa; border-radius: 6px; padding: 10px 12px; margin-top: 6px; }
.empty { padding: 18px; color: #52606d; font-size: 14px; margin: 0; }
.fail { padding: 14px 18px; color: #b3261e; font-size: 14px; margin: 0; overflow-wrap: anywhere; }
.window-title { margin: 26px 2px 10px; font-size: 15px; color: #334e68; border-left: 4px solid #2563eb; padding-left: 10px; }
.imp { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 4px; margin-right: 6px; vertical-align: 1px; }
.imp-high { background: #fdecea; color: #b3261e; }
.imp-mid { background: #fff4d5; color: #9a6700; }
.imp-low { background: #eceff3; color: #64748b; }
footer { text-align: center; color: #9aa5b1; font-size: 12px; padding: 8px 0 28px; }
@media (max-width: 640px) { .mail-head { grid-template-columns: 42px 1fr 18px; } .mail .sender { display: none; } .mail-detail { padding-left: 16px; } }
"""


def format_digest_html(
    mailboxes: list[MailboxDigest],
    generated_at: datetime,
    notes: str = '',
) -> str:
    now = (
        generated_at
        if generated_at.tzinfo
        else generated_at.astimezone()
    )
    sections = []

    def mailbox_section(box: MailboxDigest, mails: list) -> str:
        if not mails:
            return (
                '<section class="mailbox">'
                '<div class="mailbox-head">'
                f'<h2>{escape(box.display_name)}</h2>'
                '<span class="head-meta"><span class="count-badge">0 封</span></span>'
                '</div><p class="empty">过去 24 小时没有新邮件</p></section>'
            )
        mails = sorted(mails, key=_mail_sort_key)
        items = ''.join(_render_mail_list_item(mail) for mail in mails)
        actions = (
            '<span class="head-actions">'
            '<button type="button" data-toggle-all="open">展开全部</button>'
            '<button type="button" data-toggle-all="close">收起全部</button>'
            '</span>'
        )
        return (
            f'<section class="mailbox" id="{escape(box.mailbox_id)}">'
            '<div class="mailbox-head">'
            f'<h2>{escape(box.display_name)}</h2>'
            f'<span class="head-meta"><span class="count-badge">{len(mails)} 封</span>{actions}</span>'
            '</div>'
            f'<div class="mail-list">{items}</div></section>'
        )

    failed_boxes = [box for box in mailboxes if not box.ok]
    ok_boxes = [box for box in mailboxes if box.ok]
    for box in failed_boxes:
        sections.append(
            '<section class="mailbox">'
            f'<p class="fail">【{escape(box.display_name)}】读取失败'
            f'（{escape(box.status)}）：{escape(_single_line(box.message, 160))}</p>'
            '</section>'
        )
    if not ok_boxes or all(not box.emails for box in ok_boxes):
        sections.append(
            '<section class="mailbox"><p class="empty">'
            '过去 24 小时所有邮箱都没有新邮件</p></section>'
        )
    for box in ok_boxes:
        sections.append(mailbox_section(box, box.emails))
    all_ok = all(box.ok for box in mailboxes)
    banner = '全部读取成功' if all_ok else '部分邮箱读取失败'
    banner_class = 'ok' if all_ok else 'warn'
    date_text = escape(f'{generated_at:%Y-%m-%d}')
    meta_text = escape(f'{generated_at:%Y-%m-%d %H:%M:%S}')
    notes_chip = f'<span class="chip">{escape(notes)}</span>' if notes else ''
    total_mails = sum(len(box.emails) for box in ok_boxes)
    important_count = sum(
        1 for box in ok_boxes for mail in box.emails
        if getattr(mail, 'importance', '中') == '高'
    )
    stats_chip = f'<span class="chip">过去 24 小时共 {total_mails} 封</span>'
    important_chip = (
        f'<span class="chip chip-ok">重要 {important_count} 封</span>'
        if important_count
        else ''
    )
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>每日邮件摘要 · {date_text}</title>\n'
        f'<style>{HTML_STYLES}</style>\n</head>\n<body>\n'
        '<header><div class="inner"><h1>每日邮件摘要</h1>'
        '<div class="chips">'
        f'<span class="chip {banner_class}">{escape(banner)}</span>'
        f'<span class="chip">{meta_text}</span>'
        f'{stats_chip}'
        f'{important_chip}'
        f'<span class="chip">只读模式 · 未改变已读状态</span>'
        f'{notes_chip}'
        '</div></div></header>\n'
        f'<main>{"".join(sections)}</main>\n'
        '<footer>AI-Work 每日邮件摘要 · 只读生成</footer>\n'
        '<script>'
        'document.querySelectorAll(".mail-head").forEach(function(b){'
        'b.addEventListener("click",function(){'
        'b.closest(".mail").classList.toggle("open");});});'
        'document.querySelectorAll("[data-toggle-all]").forEach(function(b){'
        'b.addEventListener("click",function(){'
        'var open=b.getAttribute("data-toggle-all")==="open";'
        'b.closest("section").querySelectorAll(".mail").forEach(function(m){'
        'm.classList.toggle("open",open);});});});'
        '</script>\n'
        '</body>\n</html>\n'
    )


def build_toast_lines(mailboxes: list[MailboxDigest], generated_at: datetime) -> tuple[str, str, str]:
    title = f'每日邮件摘要 {generated_at:%m-%d}'
    counts = ' · '.join(
        f'{box.short_name} {len(box.emails) if box.ok else "✕"}封'
        for box in mailboxes
    )
    subjects = []
    for box in mailboxes:
        for mail in box.emails:
            subjects.append(_single_line(mail.subject, 28))
            if len(subjects) == 2:
                break
        if len(subjects) == 2:
            break
    return title, counts, '；'.join(subjects)


def build_toast_powershell(title: str, line2: str, line3: str) -> str:
    xml_text = (
        '<toast><visual><binding template="ToastText04">'
        f'<text id="1">{escape(title or " ")}</text>'
        f'<text id="2">{escape(line2 or " ")}</text>'
        f'<text id="3">{escape(line3 or " ")}</text>'
        '</binding></visual></toast>'
    ).replace("'", "''")
    return (
        "$AppId = '" + TOAST_APP_ID + "'\n"
        '[Windows.UI.Notifications.ToastNotificationManager, '
        'Windows.UI.Notifications, ContentType = WindowsRuntime] > $null\n'
        '[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, '
        'ContentType = WindowsRuntime] > $null\n'
        '$document = New-Object Windows.Data.Xml.Dom.XmlDocument\n'
        "$document.LoadXml('" + xml_text + "')\n"
        '$toast = New-Object Windows.UI.Notifications.ToastNotification($document)\n'
        '[Windows.UI.Notifications.ToastNotificationManager]'
        '::CreateToastNotifier($AppId).Show($toast)\n'
    )


def show_toast(title: str, line2: str, line3: str) -> bool:
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    script_path = DIGEST_DIR / 'show-toast.ps1'
    script_path.write_text(
        build_toast_powershell(title, line2, line3),
        encoding='utf-8-sig',
    )
    try:
        completed = subprocess.run(
            [
                'powershell',
                '-NoProfile',
                '-ExecutionPolicy',
                'Bypass',
                '-STA',
                '-File',
                str(script_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def write_run_artifacts(
    digest_path: Path,
    digest_text: str,
    mailboxes: list[MailboxDigest],
    generated_at: datetime,
    toast_shown: bool,
) -> None:
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(digest_text, encoding='utf-8')
    status = {
        'date': f'{generated_at:%Y-%m-%d}',
        'generated_at': generated_at.isoformat(),
        'toast_shown': toast_shown,
        'digest_path': str(digest_path),
        'mailboxes': [
            {
                'mailbox_id': box.mailbox_id,
                'status': box.status,
                'count': len(box.emails),
                'message': box.message,
            }
            for box in mailboxes
        ],
    }
    (DIGEST_DIR / 'last-run.json').write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def run_digest_update(
    *,
    open_html: bool = False,
    with_toasts: bool = True,
    start_notice: bool = False,
) -> dict[str, Any]:
    if not _acquire_run_lock():
        return {
            'ok': True,
            'skipped': True,
            'generated_at': datetime.now().astimezone().isoformat(),
            'digest_path': '',
            'opened_html': False,
            'mailbox_count': 0,
            'total_mails': 0,
            'text': '上一次运行尚未结束，本次跳过。',
        }
    try:
        ensure_environment()
        if with_toasts and start_notice:
            show_toast('邮件摘要更新中', '正在读取三个邮箱…', '完成后将自动打开页面')
        generated_at = datetime.now().astimezone()
        mailboxes = [
            collect_qq_digest(),
            collect_bachelor_digest(),
            collect_master_digest(),
        ]
        api_key = load_summary_api_key()
        enrich_digests(mailboxes, api_key)
        digest_text = format_digest(mailboxes, generated_at)
        notes = '' if api_key else 'AI 摘要与全文翻译未启用（未配置 GLM 密钥，当前显示正文预览）'
        html_text = format_digest_html(mailboxes, generated_at, notes=notes)
        digest_path = DIGEST_DIR / f'{generated_at:%Y-%m-%d}.html'
        failed_boxes = [box for box in mailboxes if not box.ok]
        toast_shown = True
        if with_toasts:
            if failed_boxes:
                failed_names = '、'.join(box.display_name for box in failed_boxes)
                toast_shown = show_toast(
                    '⚠ 邮件摘要运行异常',
                    f'读取失败：{failed_names}',
                    '打开助手页面查看详情，或稍后重试。',
                )
            else:
                toast_shown = show_toast(*build_toast_lines(mailboxes, generated_at))
        write_run_artifacts(
            digest_path, html_text, mailboxes, generated_at, toast_shown
        )
        opened_html = False
        if open_html:
            try:
                os.startfile(digest_path)  # noqa: S606 - local generated file
                opened_html = True
            except OSError:
                _LOGGER.warning('could not open digest file', exc_info=True)
        all_ok = all(box.ok for box in mailboxes)
        return {
            'ok': all_ok and toast_shown,
            'generated_at': generated_at.isoformat(),
            'digest_path': str(digest_path),
            'opened_html': opened_html,
            'mailbox_count': len(mailboxes),
            'total_mails': sum(len(box.emails) for box in mailboxes),
            'text': digest_text,
        }
    finally:
        _release_run_lock()


NOTIFIED_HIGH_PATH = DIGEST_DIR / 'high-importance-notified.json'
HIGH_NOTIFY_MAX_AGE_DAYS = 7


def _load_notified_high() -> dict[str, str]:
    try:
        loaded = json.loads(NOTIFIED_HIGH_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_notified_high(store: dict[str, str]) -> None:
    try:
        DIGEST_DIR.mkdir(parents=True, exist_ok=True)
        NOTIFIED_HIGH_PATH.write_text(
            json.dumps(store, ensure_ascii=False),
            encoding='utf-8',
        )
    except OSError:
        _LOGGER.warning('could not persist high-importance notify state')


def _prune_notified(
    store: dict[str, str],
    now: datetime,
    max_age_days: int = HIGH_NOTIFY_MAX_AGE_DAYS,
) -> dict[str, str]:
    reference = now if now.tzinfo else now.astimezone()
    kept: dict[str, str] = {}
    for key, date_text in store.items():
        try:
            marked = datetime.fromisoformat(str(date_text))
        except ValueError:
            continue
        if marked.tzinfo is None:
            marked = marked.astimezone()
        if (reference - marked).total_seconds() <= max_age_days * 86400:
            kept[key] = date_text
    return kept


def select_new_high(
    mailboxes: list[MailboxDigest], notified: set[str]
) -> list[MailboxDigest]:
    fresh: list[MailboxDigest] = []
    for box in mailboxes:
        if not box.ok:
            continue
        for mail in box.emails:
            key = _mail_cache_key(box.mailbox_id, mail)
            if getattr(mail, 'importance', '中') == '高' and key not in notified:
                fresh.append(mail)
    return fresh


def check_high_importance_mails(*, notify: bool = True) -> dict[str, Any]:
    """Hourly lightweight check; toast only NEW high-importance mails or failures."""
    if not _acquire_run_lock():
        return {'ok': True, 'skipped': True, 'new_high': 0}
    try:
        ensure_environment()
        now = datetime.now().astimezone()
        mailboxes = [
            collect_qq_digest(),
            collect_bachelor_digest(),
            collect_master_digest(),
        ]
        api_key = load_summary_api_key()
        enrich_digests(mailboxes, api_key)
        notified = _load_notified_high()
        notified = _prune_notified(notified, now)
        fresh_high = []
        for box in mailboxes:
            if not box.ok:
                continue
            for mail in box.emails:
                key = _mail_cache_key(box.mailbox_id, mail)
                if getattr(mail, 'importance', '中') != '高' or key in notified:
                    continue
                fresh_high.append(mail)
                notified[key] = now.isoformat()
        failed_boxes = [box for box in mailboxes if not box.ok]
        if notify:
            if fresh_high:
                subjects = '；'.join(
                    _single_line(mail.subject, 30) for mail in fresh_high[:2]
                )
                show_toast('⚠ 重要邮件提醒', f'{len(fresh_high)} 封高重要度邮件', subjects)
            if failed_boxes:
                names = '、'.join(box.display_name for box in failed_boxes)
                show_toast('⚠ 邮件检查异常', f'读取失败：{names}', '详见助手页面')
        _save_notified_high(notified)
        return {
            'ok': not failed_boxes,
            'skipped': False,
            'new_high': len(fresh_high),
            'failed': [box.mailbox_id for box in failed_boxes],
        }
    finally:
        _release_run_lock()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Read-only nightly summary of the three configured mailboxes.'
    )
    parser.add_argument(
        '--no-toast',
        action='store_true',
        help='write the digest without showing the desktop notification',
    )
    parser.add_argument(
        '--open-html',
        action='store_true',
        help='open the HTML digest in the default browser after writing',
    )
    parser.add_argument(
        '--check-high',
        action='store_true',
        help='lightweight hourly check; toast only new high-importance mails',
    )
    arguments = parser.parse_args(argv)

    if arguments.check_high:
        result = check_high_importance_mails()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['ok'] else 1

    result = run_digest_update(
        open_html=arguments.open_html,
        with_toasts=not arguments.no_toast,
        start_notice=True,
    )
    print(result['text'])
    if not result['ok']:
        print('本次运行存在失败项，详见助手页面或 last-run.json', file=sys.stderr)
    return 0 if result['ok'] else 1
