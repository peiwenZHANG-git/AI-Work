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
import shutil
import ssl
import subprocess
import threading
import tempfile
import time
import winreg
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape

import keyring
import requests

from windows_gui.health_events import record_health_event

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
DISMISSED_MAX_AGE_DAYS = 30
DISMISSED_MAX_KEYS = 5000
MAX_IMAP_MESSAGE_BYTES = 400_000
MAX_BODY_CHARS = 3500
MAX_AI_MAILS_PER_RUN = 25
CLASSIFICATION_POLICY_VERSION = 2
GRAPH_MESSAGES_URL = 'https://graph.microsoft.com/v1.0/me/messages'
GRAPH_REFRESH_MUTEX_NAME = 'Local\\AI-Work-MasterGraphRefresh'
ZHIPU_CHAT_ENDPOINT = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
DEFAULT_SUMMARY_MODEL = 'glm-4-flash'
SUMMARY_API_KEY_USERNAME = 'zhipu_glm_api_key'
SUMMARY_SYSTEM_PROMPT = (
    '你是邮件整理助手。用户会提供一封邮件，请只输出一个 JSON 对象，'
    '格式为 {"summary": "...", "importance": "高|中|低"}。'
    'summary 是一到两句话的简体中文要点，保留关键日期、地点或行动项；'
    'importance 必须严格按以下规则判断：明确要求收件人回复、提交、填写、'
    '确认、缴费、参加或在截止日期前完成其他事项为"高"；与学校、大学、'
    '课程、教师、考试、校园或校务相关但只需知悉、无需行动为"中"；'
    '其余所有邮件一律为"低"。是否与本人直接相关不能单独提高等级。'
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


@dataclass(frozen=True)
class AttachmentInfo:
    name: str
    content_type: str
    size_bytes: int | None = None


@dataclass
class DigestMail:
    sender: str
    subject: str
    time: str
    body_text: str = ''
    summary: str = ''
    translation: str = ''
    importance: str = '低'
    attachments: list[AttachmentInfo] = field(default_factory=list)
    source_reference: str = ''
    sender_address: str = ''


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


def exchange_master_refresh_token(
    refresh_token: str, scope: str = MASTER_GRAPH_SCOPE
) -> dict[str, Any]:
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
                'scope': scope,
            },
            timeout=15,
        )
    except requests.RequestException as error:
        raise MailboxFlowError(f'Graph 令牌刷新网络失败：{type(error).__name__}')
    try:
        payload = response.json()
    except ValueError as error:
        raise MailboxFlowError('Graph 令牌接口响应不是 JSON') from error
    if response.status_code != 200 or 'access_token' not in payload:
        raise MailboxFlowError(
            f"Graph 令牌刷新失败：{payload.get('error', 'unknown_error')}"
        )
    return payload


@contextmanager
def _graph_refresh_lock(timeout_seconds: float = 10.0):
    """Serialize refresh-token rotation across helper and scheduler processes."""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, GRAPH_REFRESH_MUTEX_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), 'could not create Graph refresh lock')
    acquired = False
    try:
        wait_result = kernel32.WaitForSingleObject(
            handle, max(0, int(timeout_seconds * 1000))
        )
        # WAIT_ABANDONED grants a mutex whose owner died. Credential Manager
        # reads/writes are independently atomic, so recovering is preferable
        # to leaving an automated digest blocked until restart.
        if wait_result not in (0, 0x00000080):
            raise TimeoutError('timed out waiting for Graph refresh lock')
        acquired = True
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


def refresh_master_graph_token(scope: str = MASTER_GRAPH_SCOPE) -> dict[str, Any]:
    """Refresh and rotate the delegated token without retaining secrets."""
    try:
        with _graph_refresh_lock():
            refresh_token = read_master_refresh_token()
            payload = exchange_master_refresh_token(refresh_token, scope)
            if payload.get('refresh_token'):
                write_master_refresh_token(payload['refresh_token'])
            return payload
    except MailboxFlowError:
        raise
    except (OSError, ValueError) as error:
        raise MailboxFlowError(f'Graph 凭据刷新失败：{type(error).__name__}') from error


@contextmanager
def graph_refresh_lock(timeout_seconds: float = 10.0):
    """Public primitive that serializes any token rotation with refreshes."""
    with _graph_refresh_lock(timeout_seconds):
        yield


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
        '用简体中文概括这封邮件的要点。重要程度必须按以下规则：'
        '需要收件人采取行动为高；学校相关但无需行动、只需知悉为中；'
        '其他所有邮件为低。'
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
    max_tokens: int = 2000,
    attempts: int = 2,
    retry_delay: float = 2.0,
) -> str:
    last_error: SummaryAPIError | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(retry_delay)
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
                    'max_tokens': max_tokens,
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
        importance = '低'
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


def _mail_dismiss_key(mailbox_id: str, mail: Any) -> str:
    source_reference = str(getattr(mail, 'source_reference', '') or '')
    if not source_reference:
        return _mail_cache_key(mailbox_id, mail)
    raw = f'{mailbox_id}|message|{source_reference}'
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


_DISMISSED_LOCK = threading.Lock()


def load_dismissed_store(store_path: Path | None = None) -> dict[str, str]:
    path = store_path or (DIGEST_DIR / 'dismissed-mail.json')
    try:
        loaded = json.loads(
            path.read_text(encoding='utf-8')
        )
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_dismissed_store(
    store: dict[str, str], store_path: Path | None = None
) -> bool:
    try:
        DIGEST_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            store_path or (DIGEST_DIR / 'dismissed-mail.json'),
            json.dumps(store, ensure_ascii=False, indent=2),
        )
        return True
    except OSError:
        _LOGGER.warning('could not persist dismissed mails')
        return False


def _prune_dismissed(
    store: dict[str, str], now: datetime, max_age_days: int = DISMISSED_MAX_AGE_DAYS
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


def dismissed_keys() -> set[str]:
    now = datetime.now().astimezone()
    return set(_prune_dismissed(load_dismissed_store(), now).keys())


def dismiss_mail_keys(
    keys: list[str],
    now: datetime | None = None,
    *,
    store_path: Path | None = None,
) -> int:
    reference = now or datetime.now().astimezone()
    with _DISMISSED_LOCK:
        store = _prune_dismissed(load_dismissed_store(store_path), reference)
        while len(store) >= DISMISSED_MAX_KEYS:
            store.pop(next(iter(store)))
        added = 0
        for key in keys or []:
            clean = str(key).strip()
            if clean and clean not in store:
                store[clean] = reference.isoformat()
                added += 1
        if not save_dismissed_store(store, store_path):
            raise OSError('could not persist dismissed mails')
        return added


def _apply_dismissed_filter(mailboxes: list[MailboxDigest]) -> int:
    dismissed = dismissed_keys()
    if not dismissed:
        return 0
    removed = 0
    for box in mailboxes:
        if not box.ok:
            continue
        kept = []
        for mail in box.emails:
            current_key = _mail_dismiss_key(box.mailbox_id, mail)
            legacy_key = _mail_cache_key(box.mailbox_id, mail)
            if current_key in dismissed or legacy_key in dismissed:
                removed += 1
                continue
            kept.append(mail)
        box.emails = kept
    return removed


_MAIL_CARD_PATTERN = re.compile(
    r'<article class="mail"\s+data-key="([0-9a-f]{40})".*?</article>',
    re.DOTALL,
)


def filter_dismissed_html(html_text: str, keys: set[str]) -> str:
    """Remove generated mail cards whose opaque keys are already dismissed."""
    if not keys:
        return html_text
    return _MAIL_CARD_PATTERN.sub(
        lambda match: '' if match.group(1) in keys else match.group(0),
        html_text,
    )


def remove_dismissed_from_latest_digest(
    keys: set[str], digest_dir: Path | None = None
) -> int:
    """Atomically remove newly dismissed cards from the current HTML artifact."""
    directory = digest_dir or DIGEST_DIR
    files = sorted(directory.glob('*.html'))
    if not files or not keys:
        return 0
    path = files[-1]
    original = path.read_text(encoding='utf-8')
    filtered = filter_dismissed_html(original, keys)
    if filtered == original:
        return 0
    removed = len(_MAIL_CARD_PATTERN.findall(original)) - len(
        _MAIL_CARD_PATTERN.findall(filtered)
    )
    _atomic_write_text(path, filtered)
    return removed


def _collect_mailbox_digests(
    collectors: tuple[Any, ...] | None = None,
) -> list[MailboxDigest]:
    """Read independent mailboxes concurrently while preserving display order."""
    selected = collectors or (
        collect_qq_digest,
        collect_bachelor_digest,
        collect_master_digest,
    )
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        return list(pool.map(lambda collect: collect(), selected))


def enrich_digests(
    mailboxes: list[MailboxDigest],
    api_key: str,
    transport: Any = None,
    cache: dict[str, Any] | None = None,
    include_translations: bool = True,
    max_workers: int = 4,
) -> None:
    persistent = cache is None
    cache = load_translation_cache() if cache is None else cache
    pending: list[tuple[MailboxDigest, Any, str, str, bool]] = []
    for box in mailboxes:
        if not box.ok:
            continue
        for mail in box.emails:
            body_text = getattr(mail, 'body_text', '')
            if not body_text:
                mail.summary = ''
                mail.translation = ''
                mail.importance = '低'
                continue
            cache_key = _mail_cache_key(box.mailbox_id, mail)
            cached = cache.get(cache_key)
            if _cache_entry_usable(cached, include_translations):
                mail.summary = str(cached.get('summary') or '')
                mail.translation = str(cached.get('translation') or '')
                mail.importance = _valid_importance(cached.get('importance'))
                continue
            needs_translation = include_translations
            if _cache_content_usable(cached, include_translations):
                mail.summary = str(cached.get('summary') or '')
                mail.translation = str(cached.get('translation') or '')
                needs_translation = False
            pending.append(
                (box, mail, cache_key, body_text, needs_translation)
            )
    if pending:
        _process_pending(
            pending,
            api_key,
            cache,
            transport,
            include_translations,
            max_workers,
            persistent,
        )
    for box in mailboxes:
        _sort_box_mails(box)
    if persistent:
        save_translation_cache(cache)


def _cache_entry_usable(entry: Any, require_translation: bool) -> bool:
    return (
        _cache_content_usable(entry, require_translation)
        and entry.get('classification_policy_version')
        == CLASSIFICATION_POLICY_VERSION
    )


def _cache_content_usable(entry: Any, require_translation: bool) -> bool:
    if not isinstance(entry, dict) or 'summary' not in entry:
        return False
    summary = str(entry.get('summary') or '')
    if not _contains_cjk(summary):
        return False
    if require_translation:
        return _contains_cjk(str(entry.get('translation') or ''))
    return True


def _valid_importance(value: Any) -> str:
    text = str(value or '低')
    return text if text in VALID_IMPORTANCE else '低'


def _process_pending(
    pending: list[tuple[MailboxDigest, Any, str, str, bool]],
    api_key: str,
    cache: dict[str, Any],
    transport: Any,
    include_translations: bool,
    max_workers: int,
    persistent: bool,
) -> None:
    remaining = [MAX_AI_MAILS_PER_RUN]
    failure_count = [0]
    cancel_event = threading.Event()
    state_lock = threading.Lock()

    def apply_preview(mail: Any, body_text: str) -> None:
        if not getattr(mail, 'summary', ''):
            mail.summary = '正文预览：' + _single_line(body_text, 140)
            mail.translation = ''
        mail.importance = '低'

    def process_task(task: tuple[MailboxDigest, Any, str, str, bool]) -> None:
        box, mail, cache_key, body_text, needs_translation = task
        with state_lock:
            if cancel_event.is_set() or remaining[0] <= 0:
                apply_preview(mail, body_text)
                return
        try:
            mail.summary, mail.importance = call_mail_summary(
                mail, api_key, transport=transport
            )
            if needs_translation:
                mail.translation = call_mail_translation(
                    body_text, api_key, transport=transport
                )
            elif not include_translations:
                mail.translation = ''
        except SummaryAPIError as error:
            with state_lock:
                failure_count[0] += 1
                if failure_count[0] >= 3:
                    cancel_event.set()
            _LOGGER.warning('AI 处理失败：%s', error)
            apply_preview(mail, body_text)
            return
        cacheable = include_translations and _contains_cjk(
            str(mail.translation or '')
        )
        with state_lock:
            remaining[0] -= 1
            if cacheable:
                cache[cache_key] = {
                    'summary': mail.summary,
                    'translation': mail.translation,
                    'importance': mail.importance,
                    'classification_policy_version': (
                        CLASSIFICATION_POLICY_VERSION
                    ),
                }

    workers = min(max_workers, len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(process_task, pending))


def _encoded_attachment_size(part: Any) -> int | None:
    raw_length = str(part.get('Content-Length') or '').strip()
    if raw_length.isdigit():
        return int(raw_length)
    payload = part.get_payload(decode=False)
    if isinstance(payload, bytes):
        return len(payload)
    if not isinstance(payload, str):
        return None
    encoding = str(part.get('Content-Transfer-Encoding') or '').casefold()
    if encoding == 'base64':
        compact = ''.join(payload.split())
        if not compact:
            return 0
        return max(0, (len(compact) * 3 // 4) - compact[-2:].count('='))
    return len(payload.encode('utf-8')) if payload else 0


def extract_attachment_metadata(message: Any) -> list[AttachmentInfo]:
    """Read MIME attachment headers without decoding or opening payloads."""
    attachments = []
    for part in message.walk():
        disposition = str(part.get_content_disposition() or '').casefold()
        filename = str(part.get_filename() or '').strip()
        if disposition == 'inline' or not (
            disposition == 'attachment' or filename
        ):
            continue
        attachments.append(AttachmentInfo(
            name=_single_line(filename or '未命名附件', 160),
            content_type=_single_line(
                str(part.get_content_type() or 'application/octet-stream'), 120
            ),
            size_bytes=_encoded_attachment_size(part),
        ))
        if len(attachments) >= 20:
            break
    return attachments


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
        sender_address=address,
        subject=str(message.get('Subject') or ''),
        time=time_text,
        body_text=extract_body_text(message),
        attachments=extract_attachment_metadata(message),
        source_reference=f'imap:{item.uid.decode("ascii", errors="ignore")}',
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


def _graph_attachment_metadata(
    message_id: str,
    access_token: str,
    *,
    transport: Any = None,
) -> list[AttachmentInfo]:
    """Fetch Graph attachment metadata only; never request contentBytes."""
    if not message_id:
        return []
    get = transport or requests.get
    params = urlencode({'$select': 'name,contentType,size,isInline'})
    try:
        response = get(
            f'{GRAPH_MESSAGES_URL}/{quote(message_id, safe="")}/attachments?{params}',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=20,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        values = response.json().get('value', [])
    except (AttributeError, ValueError):
        return []
    attachments = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict) or item.get('isInline') is True:
            continue
        name = _single_line(str(item.get('name') or '未命名附件'), 160)
        content_type = _single_line(
            str(item.get('contentType') or 'application/octet-stream'), 120
        )
        try:
            size_bytes = max(0, int(item.get('size')))
        except (TypeError, ValueError):
            size_bytes = None
        attachments.append(AttachmentInfo(name, content_type, size_bytes))
        if len(attachments) >= 20:
            break
    return attachments


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
        payload = refresh_master_graph_token()
    except MailboxFlowError as error:
        status = (
            BackendStatus.NOT_AUTHENTICATED.value
            if 'invalid_grant' in str(error)
            or '未找到登录授权' in str(error)
            or '环境变量未配置' in str(error)
            else BackendStatus.REQUEST_FAILED.value
        )
        return failed(status, str(error))

    config = GraphBackendConfig.from_environment()
    if not config.is_configured:
        return failed(BackendStatus.NOT_AUTHENTICATED, 'Graph 环境变量不完整')

    params = urlencode({
        '$top': MAX_FETCH_PER_MAILBOX,
        '$select': 'id,subject,sender,receivedDateTime,body,hasAttachments',
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
    attachment_jobs: list[tuple[DigestMail, str]] = []
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
        mail = DigestMail(
            sender=str(sender_info.get('name') or sender_info.get('address') or ''),
            sender_address=str(sender_info.get('address') or ''),
            subject=str(item.get('subject') or ''),
            time=received_raw,
            body_text=graph_body_text(item.get('body')),
            source_reference=(
                f'graph:{item["id"]}' if item.get('id') else ''
            ),
        )
        mails.append(mail)
        if item.get('hasAttachments') is True and item.get('id'):
            attachment_jobs.append((mail, str(item['id'])))

    if attachment_jobs:
        def fetch_attachments(job: tuple[DigestMail, str]) -> None:
            mail, message_id = job
            mail.attachments = _graph_attachment_metadata(
                message_id, str(payload['access_token'])
            )

        with ThreadPoolExecutor(
            max_workers=min(4, len(attachment_jobs))
        ) as pool:
            list(pool.map(fetch_attachments, attachment_jobs))

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


def _escape_attr(value: Any) -> str:
    return escape(str(value or ''), {'"': '&quot;', "'": '&#x27;'})


def _format_attachment_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return '大小未知'
    if size_bytes < 1024:
        return f'{size_bytes} B'
    if size_bytes < 1024 * 1024:
        return f'{size_bytes / 1024:.1f} KB'
    return f'{size_bytes / (1024 * 1024):.1f} MB'


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
    rank = IMPORTANCE_RANK.get(getattr(mail, 'importance', '低'), 2)
    return (rank, -timestamp)


def _sort_box_mails(box: MailboxDigest) -> None:
    box.emails.sort(key=_mail_sort_key)


def _render_mail_list_item(mail: Any, mailbox_id: str) -> str:
    summary = getattr(mail, 'summary', '')
    translation = getattr(mail, 'translation', '')
    body_text = getattr(mail, 'body_text', '')
    importance = getattr(mail, 'importance', '低')
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
    attachments = list(getattr(mail, 'attachments', []) or [])
    attachment_html = ''
    if attachments:
        rows = ''.join(
            '<li><span class="attachment-name">'
            f'{escape(_single_line(item.name, 160))}</span>'
            f'<span class="attachment-meta">{escape(item.content_type)} · '
            f'{escape(_format_attachment_size(item.size_bytes))}</span></li>'
            for item in attachments
        )
        attachment_html = (
            f'<div class="attachments"><h3 class="detail-title">附件元数据'
            f'（{len(attachments)}）</h3><ul>{rows}</ul>'
            '<p class="attachment-note">仅显示名称、类型和大小；未打开或保存附件。</p>'
            '</div>'
        )
    parsed_date = _mail_dt(getattr(mail, 'time', ''))
    date_text = parsed_date.strftime('%Y-%m-%d') if parsed_date else ''
    search_text = ' '.join([
        str(getattr(mail, 'sender', '') or ''),
        str(getattr(mail, 'subject', '') or ''),
        str(summary or ''),
        ' '.join(item.name for item in attachments),
        ' '.join(item.content_type for item in attachments),
    ]).casefold()
    return (
        f'<article class="mail" data-key="{_escape_attr(_mail_dismiss_key(mailbox_id, mail))}" '
        f'data-mailbox="{_escape_attr(mailbox_id)}" '
        f'data-sender="{_escape_attr(mail.sender)}" '
        f'data-sender-address="{_escape_attr(getattr(mail, "sender_address", ""))}" '
        f'data-subject="{_escape_attr(mail.subject)}" '
        f'data-importance="{_escape_attr(importance)}" '
        f'data-date="{_escape_attr(date_text)}" '
        f'data-search="{_escape_attr(search_text)}">'
        '<button class="mail-head" type="button">'
        f'<span class="time">{escape(_mail_time(mail.time))}</span>'
        f'<span class="sender">{escape(_single_line(mail.sender, 60))}</span>'
        f'<span class="subject"><span class="imp {imp_class}">'
        f'{escape(importance)}</span>{escape(str(mail.subject or ""))}</span>'
        '<svg class="chevron" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
        '<path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.6" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        '</button>'
        '<span class="mail-dismiss-wrap">'
        '<button class="mail-reply" type="button" '
        f'data-reply-key="{_escape_attr(_mail_dismiss_key(mailbox_id, mail))}" '
        'title="用 AI 生成回复草稿；不会发送邮件">AI 回复</button>'
        '<button class="mail-dismiss" type="button" '
        'title="标记为已处理：从摘要中隐藏，不影响邮箱里的已读状态">已读</button>'
        '</span>'
        f'{summary_html}'
        f'<div class="mail-detail">{attachment_html}{translation_html}{original_html}</div>'
        '</article>'
    )


class _DigestCardParser(HTMLParser):
    """Extract the bounded, already-rendered cards from a digest artifact."""

    _TEXT_CLASSES = {
        'time', 'sender', 'subject', 'mail-summary', 'translation',
        'original-body', 'attachment-name',
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, Any]] = []
        self._card_depth = 0
        self._current: dict[str, Any] | None = None
        self._text_class: str | None = None
        self._text_depth = 0
        self._text_parts: list[str] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        values = dict(attrs).get('class') or ''
        return set(values.split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == 'article' and 'mail' in classes and self._current is None:
            self._card_depth = 1
            raw = dict(attrs)
            self._current = {
                'key': str(raw.get('data-key') or ''),
                'mailbox_id': str(raw.get('data-mailbox') or ''),
                'sender': str(raw.get('data-sender') or ''),
                'sender_address': str(raw.get('data-sender-address') or ''),
                'subject': str(raw.get('data-subject') or ''),
                'importance': str(raw.get('data-importance') or '低'),
                'date': str(raw.get('data-date') or ''),
                'time': '',
                'summary': '',
                'translation': '',
                'body': '',
                'attachments': [],
            }
            return
        if self._current and classes.intersection(self._TEXT_CLASSES):
            self._text_class = next(
                name for name in classes if name in self._TEXT_CLASSES
            )
            self._text_depth = 1
            self._text_parts = []
        elif self._text_depth:
            if tag not in {
                'br', 'img', 'input', 'meta', 'link', 'hr',
            }:
                self._text_depth += 1

    def handle_data(self, data: str) -> None:
        if self._text_class is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._text_class is not None:
            self._text_depth -= 1
            if self._text_depth <= 0:
                text = ' '.join(''.join(self._text_parts).split())
                assert self._current is not None
                if self._text_class == 'mail-summary':
                    self._current['summary'] = text
                elif self._text_class == 'time':
                    self._current['time'] = text
                elif self._text_class == 'attachment-name':
                    if text:
                        self._current['attachments'].append(text)
                elif self._text_class == 'translation':
                    self._current['translation'] = text
                elif self._text_class == 'original-body':
                    self._current['body'] = text
                elif self._text_class == 'subject':
                    for importance in VALID_IMPORTANCE:
                        if text.startswith(importance):
                            text = text[len(importance):].strip()
                            break
                    self._current['_visible_subject'] = text
                self._text_class = None
                self._text_depth = 0
                self._text_parts = []
        if self._card_depth:
            if tag == 'article' and self._current is not None:
                self._card_depth -= 1
                if self._card_depth == 0 and self._current is not None:
                    # Older artifacts do not carry machine-readable sender and
                    # subject attributes; fall back to their visible text only.
                    if not self._current.get('subject'):
                        visible_subject = self._current.get('_visible_subject', '')
                        self._current['subject'] = visible_subject
                    self.cards.append(self._current)
                    self._current = None


def extract_digest_mail_cards(html_text: str) -> list[dict[str, Any]]:
    """Read mail cards from a generated digest without contacting a mailbox."""
    parser = _DigestCardParser()
    parser.feed(str(html_text or ''))
    parser.close()
    cards = []
    for item in parser.cards:
        item.pop('_visible_subject', None)
        cards.append(item)
    return cards


def _digest_card_due_date(card: dict[str, Any]) -> date | None:
    text = ' '.join(str(card.get(key) or '') for key in (
        'subject', 'summary', 'translation', 'body',
    ))
    today = date.today()
    patterns = (
        (r'(\d{4})-(\d{1,2})-(\d{1,2})', 'ymd'),
        (r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', 'ymd'),
        (r'(\d{1,2})\s*月\s*(\d{1,2})\s*日', 'md'),
        (r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', 'dmy'),
    )
    for pattern, kind in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        try:
            if kind == 'ymd':
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            if kind == 'md':
                return date(today.year, int(match.group(1)), int(match.group(2)))
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            continue
    return None


def _classify_digest_card(card: dict[str, Any]) -> dict[str, Any] | None:
    subject = str(card.get('subject') or '')
    summary = str(card.get('summary') or '')
    content = ' '.join((
        subject,
        summary,
        str(card.get('translation') or ''),
        str(card.get('body') or ''),
    ))
    importance = str(card.get('importance') or '低')
    action_terms = (
        '回复', '答复', '确认', '提交', '填写', '缴费', '付款', '报名',
        '申请', '预约', '参加', '上传', '签字', 'respond', 'reply',
        'confirm', 'submit', 'register', 'pay',
    )
    deadline_terms = ('截止', '期限', 'deadline', 'before')
    school_terms = (
        '学校', '大学', '学院', '教务', '行政', '注册', '学费', '奖学金',
        'campus', 'university', 'université', 'école', 'scolarité',
    )
    due_date = _digest_card_due_date(card)
    is_deadline = (
        due_date is not None
        or any(term in content.casefold() for term in deadline_terms)
    )
    is_action = any(term in content.casefold() for term in action_terms)
    is_school = any(term in content.casefold() for term in school_terms)
    is_important = importance == '高'
    if not any((is_deadline, is_action, is_school, is_important)):
        return None
    if is_deadline:
        item_type = 'deadline'
        reason = '存在截止日期或办理期限'
    elif is_action:
        item_type = 'action'
        reason = '疑似需要回复或办理'
    elif is_school:
        item_type = 'school_admin'
        reason = '学校或行政通知'
    else:
        item_type = 'important'
        reason = 'AI 摘要判定为高重要度'
    return {
        'key': card.get('key'),
        'mailbox_id': card.get('mailbox_id'),
        'type': item_type,
        'reason': reason,
        'importance': importance,
        'due_date': due_date.isoformat() if due_date else None,
        'sender': str(card.get('sender') or ''),
        'sender_address': str(card.get('sender_address') or ''),
        'subject': subject,
        'time': ' '.join(filter(None, (card.get('date'), card.get('time')))),
        'summary': _single_line(summary or content, 240),
    }


def build_today_action_items(html_text: str, limit: int = 8) -> dict[str, Any]:
    """Build a concise local worklist from the latest READ-only digest."""
    if not 1 <= limit <= 20:
        raise ValueError('limit must be between 1 and 20')
    cards = extract_digest_mail_cards(html_text)
    classified = [
        item for item in (
            _classify_digest_card(card) for card in cards
        )
        if item is not None
    ]
    today = date.today()

    def sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        due = item.get('due_date')
        try:
            due_key = date.fromisoformat(str(due)).toordinal() if due else 99999999
        except ValueError:
            due_key = 99999999
        overdue = 0 if due and date.fromisoformat(str(due)) < today else 1
        return (
            overdue,
            IMPORTANCE_RANK.get(item.get('importance'), 2),
            str(item.get('time') or ''),
        )

    classified.sort(key=sort_key)
    return {
        'item_count': len(classified),
        'items': classified[:limit],
        'read_state_change': 'NONE',
        'source': 'latest_local_digest',
    }


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
            importance = getattr(mail, 'importance', '低')
            lines.append(
                f'  {index}. [{importance}] {_mail_time(mail.time)} '
                f'{_single_line(mail.sender, 40)} — {_single_line(mail.subject, 70)}'
            )
            summary = getattr(mail, 'summary', '')
            if summary:
                lines.append(f'     ↳ {_single_line(summary, 110)}')
            attachments = list(getattr(mail, 'attachments', []) or [])
            if attachments:
                attachment_text = '；'.join(
                    f'{_single_line(item.name, 50)} '
                    f'({item.content_type}, {_format_attachment_size(item.size_bytes)})'
                    for item in attachments
                )
                lines.append(f'     附件：{_single_line(attachment_text, 180)}')
        lines.append('')
    lines.append('只读模式：正文仅用于生成摘要；卡片可标记"已读"仅隐藏显示')
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
.filters { display: grid; grid-template-columns: minmax(180px, 1fr) repeat(3, minmax(120px, auto)) auto; gap: 8px; align-items: center; background: #fff; border: 1px solid #dfe3e8; border-radius: 8px; padding: 12px; margin-bottom: 16px; }
.filters input, .filters select, .filters button { min-height: 34px; border: 1px solid #d6dce3; border-radius: 5px; background: #fff; color: #243b53; padding: 5px 9px; font: inherit; font-size: 13px; }
.filters button { cursor: pointer; background: #f6f8fa; }
.filter-count { grid-column: 1 / -1; color: #7b8794; font-size: 12px; }
section.mailbox { background: #ffffff; border: 1px solid #dfe3e8; border-radius: 8px; margin-bottom: 18px; overflow: hidden; }
.mailbox-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 14px 18px; border-bottom: 1px solid #eef1f4; }
.mailbox-head h2 { margin: 0; font-size: 16px; }
.head-meta { display: flex; align-items: center; gap: 8px; }
.count-badge { font-size: 12px; color: #52606d; background: #f1f4f7; border: 1px solid #e0e4e8; padding: 2px 10px; border-radius: 4px; }
.head-actions { display: flex; gap: 6px; }
.head-actions button { font: inherit; font-size: 12px; color: #334e68; background: #f6f8fa; border: 1px solid #dfe3e8; border-radius: 4px; padding: 3px 10px; cursor: pointer; }
.head-actions button:hover { background: #eef2f6; }
.mail-list { list-style: none; margin: 0; padding: 0; }
article.mail { position: relative; }
article.mail + article.mail { border-top: 1px solid #eef1f4; }
.mail-head { display: grid; grid-template-columns: 46px minmax(110px, 200px) 1fr 18px; gap: 10px; align-items: start; width: 100%; padding: 10px 92px 10px 14px; background: transparent; border: 0; font: inherit; text-align: left; cursor: pointer; }
.mail-head:hover { background: #f6f8fa; }
.mail .time { color: #7b8794; font-size: 13px; font-variant-numeric: tabular-nums; padding-top: 1px; }
.mail .sender { color: #486581; font-size: 13px; overflow-wrap: anywhere; padding-top: 1px; }
.mail .subject { color: #1f2430; font-size: 14px; overflow-wrap: anywhere; }
.chevron { width: 14px; height: 14px; margin-top: 3px; color: #829ab1; transition: transform 0.15s ease; }
.mail.open .chevron { transform: rotate(180deg); }
.mail-dismiss-wrap { position: absolute; top: 8px; right: 36px; display: flex; gap: 5px; }
.mail-dismiss, .mail-reply { font: inherit; font-size: 11px; padding: 2px 8px; border-radius: 4px; border: 1px solid #dbe1e8; background: #fff; color: #8792a2; cursor: pointer; }
.mail-reply { color: #1d4ed8; }
.mail-dismiss:hover { color: #b3261e; border-color: #b3261e; background: #fdecea; }
.mail-reply:hover { border-color: #1d4ed8; background: #eff6ff; }
.mail-detail { display: none; padding: 4px 16px 16px 70px; background: #fbfcfd; border-top: 1px solid #eef1f4; }
.mail.open .mail-detail { display: block; }
.mail-summary { margin: 6px 14px 10px 70px; font-size: 13px; color: #243b53; background: #f0f6ff; border: 1px solid #d6e4ff; border-radius: 6px; padding: 6px 10px; }
.mail-summary .label { color: #2563eb; font-weight: 600; margin-right: 6px; }
.detail-title { margin: 0 0 6px; font-size: 13px; color: #52606d; }
.attachments { margin-bottom: 14px; padding: 9px 11px; border: 1px solid #dfe7ef; border-radius: 6px; background: #fff; }
.attachments ul { margin: 0; padding-left: 18px; }
.attachments li { margin: 4px 0; }
.attachment-name { color: #243b53; font-size: 13px; overflow-wrap: anywhere; }
.attachment-meta { color: #7b8794; font-size: 12px; margin-left: 7px; }
.attachment-note { margin: 7px 0 0; color: #9aa5b1; font-size: 11px; }
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
@media (max-width: 640px) { .filters { grid-template-columns: 1fr 1fr; } .filters .filter-search, .filter-count { grid-column: 1 / -1; } .mail-head { grid-template-columns: 42px 1fr 18px; } .mail .sender { display: none; } .mail-detail { padding-left: 16px; } }
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
        items = ''.join(
            _render_mail_list_item(mail, box.mailbox_id) for mail in mails
        )
        actions = (
            '<span class="head-actions">'
            '<button type="button" data-toggle-all="open">展开全部</button>'
            '<button type="button" data-toggle-all="close">收起全部</button>'
            '</span>'
        )
        return (
            f'<section class="mailbox" id="{escape(box.mailbox_id)}" data-mail-section>'
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
        if getattr(mail, 'importance', '低') == '高'
    )
    stats_chip = f'<span class="chip">过去 24 小时共 {total_mails} 封</span>'
    important_chip = (
        f'<span class="chip chip-ok">重要 {important_count} 封</span>'
        if important_count
        else ''
    )
    mailbox_options = ''.join(
        f'<option value="{_escape_attr(box.mailbox_id)}">'
        f'{escape(box.display_name)}</option>'
        for box in ok_boxes
    )
    filters_html = (
        '<div class="filters" aria-label="邮件筛选">'
        '<input class="filter-search" id="filter-search" type="search" '
        'placeholder="搜索发件人、主题、摘要或附件名">'
        '<select id="filter-mailbox"><option value="">全部邮箱</option>'
        f'{mailbox_options}</select>'
        '<select id="filter-importance"><option value="">全部重要程度</option>'
        '<option value="高">高</option><option value="中">中</option>'
        '<option value="低">低</option></select>'
        '<input id="filter-date" type="date" aria-label="按日期筛选">'
        '<button id="filter-reset" type="button">清除筛选</button>'
        f'<span class="filter-count" id="filter-count">显示 {total_mails} 封</span>'
        '</div>'
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
        f'<span class="chip">只读模式 · 卡片可标记"已读"隐藏</span>'
        f'{notes_chip}'
        '</div></div></header>\n'
        f'<main>{filters_html}{"".join(sections)}</main>\n'
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
        'document.querySelectorAll(".mail-dismiss").forEach(function(b){'
        'b.addEventListener("click",async function(e){'
        'e.stopPropagation();'
        'var card=b.closest(".mail");if(!card)return;'
        'var key=card.getAttribute("data-key")||"";'
        'b.disabled=true;b.textContent="保存中";'
        'try{var response=await fetch("/api/dismiss",{method:"POST",'
        'headers:{"Content-Type":"application/json"},'
        'body:JSON.stringify({keys:[key]})});'
        'if(!response.ok)throw new Error("dismiss_failed");'
        'card.style.transition="opacity 0.25s ease";card.style.opacity="0";'
        'setTimeout(function(){card.remove();applyFilters();},250);'
        '}catch(error){b.disabled=false;b.textContent="重试";'
        'b.title="保存失败，请重试";}});});'
        'document.querySelectorAll(".mail-reply").forEach(function(b){'
        'b.addEventListener("click",function(e){e.stopPropagation();'
        'var card=b.closest(".mail");if(!card)return;'
        'var payload={type:"ai-reply",key:card.getAttribute("data-key")||"",'
        'label:(card.querySelector(".sender")||{}).textContent||"已选择邮件"};'
        'if(window.parent!==window){window.parent.postMessage(payload,window.location.origin);}'
        'else{sessionStorage.setItem("ai-reply",JSON.stringify(payload));window.location.href="/";}});});'
        'function applyFilters(){'
        'var q=(document.getElementById("filter-search").value||"").toLowerCase().trim();'
        'var mailbox=document.getElementById("filter-mailbox").value;'
        'var importance=document.getElementById("filter-importance").value;'
        'var date=document.getElementById("filter-date").value;var visible=0;'
        'document.querySelectorAll(".mail").forEach(function(m){'
        'var show=(!q||(m.dataset.search||"").includes(q))'
        '&&(!mailbox||m.dataset.mailbox===mailbox)'
        '&&(!importance||m.dataset.importance===importance)'
        '&&(!date||m.dataset.date===date);'
        'm.style.display=show?"":"none";if(show)visible++;});'
        'document.querySelectorAll("[data-mail-section]").forEach(function(s){'
        'var cards=Array.from(s.querySelectorAll(".mail"));'
        'var shown=cards.filter(function(m){return m.style.display!=="none";}).length;'
        's.style.display=shown?"":"none";'
        'var badge=s.querySelector(".count-badge");if(badge)badge.textContent=shown+" / "+cards.length+" 封";});'
        'document.getElementById("filter-count").textContent="显示 "+visible+" 封";}'
        '["filter-search","filter-mailbox","filter-importance","filter-date"].forEach(function(id){'
        'document.getElementById(id).addEventListener("input",applyFilters);});'
        'document.getElementById("filter-reset").addEventListener("click",function(){'
        '["filter-search","filter-mailbox","filter-importance","filter-date"].forEach(function(id){document.getElementById(id).value="";});applyFilters();});'
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
    status_path: Path | None = None,
) -> None:
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(digest_path, digest_text)
    mailboxes_ok = all(box.ok for box in mailboxes)
    status = {
        'date': f'{generated_at:%Y-%m-%d}',
        'generated_at': generated_at.isoformat(),
        'ok': mailboxes_ok and toast_shown,
        'mailboxes_ok': mailboxes_ok,
        'toast_shown': toast_shown,
        'digest_path': str(digest_path),
        'mailbox_count': len(mailboxes),
        'total_mails': sum(len(box.emails) for box in mailboxes),
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
    _atomic_write_text(
        status_path or (DIGEST_DIR / 'last-run.json'),
        json.dumps(status, ensure_ascii=False, indent=2),
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.',
        suffix='.tmp',
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows can transiently reject an atomic replace when another local
        # reader holds the destination. A fully-written temp file still allows
        # a non-atomic fallback so diagnostics/status are not silently lost.
        for replace_attempt in range(3):
            try:
                os.replace(temporary_path, path)
                break
            except OSError:
                if replace_attempt == 2:
                    shutil.copyfile(temporary_path, path)
                else:
                    time.sleep(0.02 * (replace_attempt + 1))
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def write_attempt_artifact(
    stage: str,
    *,
    ok: bool,
    generated_at: datetime,
    skipped: bool = False,
    reason: str | None = None,
    mailboxes: list[MailboxDigest] | None = None,
    mailbox_count: int | None = None,
    total_mails: int | None = None,
    artifact_written: bool | None = None,
    artifact_error: str | None = None,
    error_type: str | None = None,
    attempt_path: Path | None = None,
) -> bool:
    """Persist non-sensitive run diagnostics without relying on last-run."""
    mailbox_status = [
        {
            'mailbox_id': box.mailbox_id,
            'status': box.status,
            'count': len(box.emails),
        }
        for box in (mailboxes or [])
    ]
    payload: dict[str, Any] = {
        'generated_at': generated_at.isoformat(),
        'stage': stage,
        'ok': ok,
        'skipped': skipped,
        'mailbox_count': mailbox_count if mailbox_count is not None else len(mailbox_status),
        'total_mails': total_mails or 0,
        'mailboxes': mailbox_status,
    }
    if reason is not None:
        payload['reason'] = reason
    if artifact_written is not None:
        payload['artifact_written'] = artifact_written
    if artifact_error is not None:
        payload['artifact_error'] = artifact_error
    if error_type is not None:
        payload['error_type'] = error_type
    try:
        _atomic_write_text(
            attempt_path or (DIGEST_DIR / 'last-attempt.json'),
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        return True
    except OSError:
        _LOGGER.exception('could not persist digest attempt diagnostics')
        return False


def run_digest_update(
    *,
    open_html: bool = False,
    with_toasts: bool = True,
    start_notice: bool = False,
    attempt_path: Path | None = None,
) -> dict[str, Any]:
    if not _acquire_run_lock():
        generated_at = datetime.now().astimezone()
        write_attempt_artifact(
            'lock_busy',
            ok=False,
            generated_at=generated_at,
            skipped=True,
            reason='lock_busy',
            attempt_path=attempt_path,
        )
        return {
            'ok': False,
            'skipped': True,
            'reason': 'lock_busy',
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
        mailboxes = _collect_mailbox_digests()
        _apply_dismissed_filter(mailboxes)
        mailboxes_ok = all(box.ok for box in mailboxes)
        write_attempt_artifact(
            'mailboxes_read',
            ok=mailboxes_ok,
            generated_at=generated_at,
            mailboxes=mailboxes,
            total_mails=sum(len(box.emails) for box in mailboxes),
            attempt_path=attempt_path,
        )
        api_key = load_summary_api_key()
        enrich_digests(mailboxes, api_key)
        # A user can dismiss a card while AI enrichment is still running.
        # Re-read the store immediately before rendering to close that race.
        _apply_dismissed_filter(mailboxes)
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
        try:
            write_run_artifacts(
                digest_path, html_text, mailboxes, generated_at, toast_shown
            )
        except OSError as error:
            _LOGGER.exception('could not persist digest artifacts')
            write_attempt_artifact(
                'artifact_write',
                ok=False,
                generated_at=generated_at,
                mailboxes=mailboxes,
                total_mails=sum(len(box.emails) for box in mailboxes),
                artifact_written=False,
                artifact_error=type(error).__name__,
                attempt_path=attempt_path,
            )
            record_health_event('mail_digest', 'error', 'digest_failed')
            return {
                'ok': False,
                'artifact_written': False,
                'artifact_error': type(error).__name__,
                'generated_at': generated_at.isoformat(),
                'digest_path': str(digest_path),
                'opened_html': False,
                'mailbox_count': len(mailboxes),
                'total_mails': sum(len(box.emails) for box in mailboxes),
                'text': digest_text,
            }
        opened_html = False
        if open_html:
            try:
                os.startfile(digest_path)  # noqa: S606 - local generated file
                opened_html = True
            except OSError:
                _LOGGER.warning('could not open digest file', exc_info=True)
        all_ok = all(box.ok for box in mailboxes)
        write_attempt_artifact(
            'complete',
            ok=all_ok and toast_shown,
            generated_at=generated_at,
            mailboxes=mailboxes,
            total_mails=sum(len(box.emails) for box in mailboxes),
            artifact_written=True,
            attempt_path=attempt_path,
        )
        if all_ok and toast_shown:
            record_health_event('mail_digest', 'success', 'digest_completed')
        elif not all_ok:
            record_health_event('mail_digest', 'error', 'digest_failed')
        else:
            record_health_event(
                'mail_digest', 'warning', 'digest_notification_warning'
            )
        return {
            'ok': all_ok and toast_shown,
            'artifact_written': True,
            'generated_at': generated_at.isoformat(),
            'digest_path': str(digest_path),
            'opened_html': opened_html,
            'mailbox_count': len(mailboxes),
            'total_mails': sum(len(box.emails) for box in mailboxes),
            'text': digest_text,
        }
    except Exception as error:
        write_attempt_artifact(
            'exception',
            ok=False,
            generated_at=datetime.now().astimezone(),
            error_type=type(error).__name__,
            attempt_path=attempt_path,
        )
        record_health_event('mail_digest', 'error', 'digest_failed')
        raise
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
            if getattr(mail, 'importance', '低') == '高' and key not in notified:
                fresh.append(mail)
    return fresh


def check_high_importance_mails(*, notify: bool = True) -> dict[str, Any]:
    """Hourly lightweight check; toast only NEW high-importance mails or failures."""
    if not _acquire_run_lock():
        return {'ok': True, 'skipped': True, 'new_high': 0}
    try:
        ensure_environment()
        now = datetime.now().astimezone()
        mailboxes = _collect_mailbox_digests()
        _apply_dismissed_filter(mailboxes)
        api_key = load_summary_api_key()
        enrich_digests(
            mailboxes,
            api_key,
            include_translations=False,
            cache=load_translation_cache(),
        )
        notified = _load_notified_high()
        notified = _prune_notified(notified, now)
        fresh_high = []
        for box in mailboxes:
            if not box.ok:
                continue
            for mail in box.emails:
                key = _mail_cache_key(box.mailbox_id, mail)
                if getattr(mail, 'importance', '低') != '高' or key in notified:
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
