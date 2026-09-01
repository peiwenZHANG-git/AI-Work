"""AI mail assistant: natural-language drafts, draft saving, and sending.

Drafts are saved only after they are generated and shown to the user; sending
always requires an explicit confirmation in the assistant page. QQ mailbox can
save drafts but can never send (project safety rule).
"""

from __future__ import annotations

import email.utils
import hashlib
import imaplib
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from email.parser import BytesParser
from email import policy
from email.message import EmailMessage
from urllib.parse import quote
from typing import Any

import requests

from windows_gui.imap_mail import (
    BACHELOR_IMAP_CREDENTIAL_SERVICE,
    BACHELOR_IMAP_CREDENTIAL_USERNAME,
    BACHELOR_IMAP_HOST,
    BACHELOR_IMAP_PORT,
    QQ_IMAP_CREDENTIAL_SERVICE,
    QQ_IMAP_CREDENTIAL_USERNAME,
    QQ_IMAP_HOST,
    QQ_IMAP_PORT,
    _default_imap_factory,
)
from windows_gui.mail_digest import (
    MailboxFlowError,
    MASTER_GRAPH_SCOPE,
    SummaryAPIError,
    _chat_once,
    _resolve_model,
    ensure_environment,
    load_summary_api_key,
    refresh_master_graph_token,
)


ASSISTANT_SAVE_GRAPH_SCOPE = (
    'https://graph.microsoft.com/Mail.ReadWrite '
    'offline_access'
)
ASSISTANT_SEND_GRAPH_SCOPE = (
    'https://graph.microsoft.com/Mail.ReadWrite '
    'https://graph.microsoft.com/Mail.Send offline_access'
)
GRAPH_MESSAGES_URL = 'https://graph.microsoft.com/v1.0/me/messages'
GRAPH_ME_URL = 'https://graph.microsoft.com/v1.0/me'
SMTP_HOSTS = {
    'bachelor_mail': ('smtp.qiye.163.com', 465),
}
SEND_DISABLED_MAILBOXES = {'qq_mail'}
ASSISTANT_CREDENTIAL_SERVICE = 'AI-Work/windows-gui/mailboxes'
ASSISTANT_DRAFT_CREDENTIAL_USERNAMES = {
    'qq_mail': 'qq_mail_assistant_draft_authorization_code',
    'bachelor_mail': 'bachelor_mail_assistant_draft_authorization_code',
}
BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME = (
    'bachelor_mail_assistant_smtp_authorization_code'
)
DRAFT_SYSTEM_PROMPT = (
    '你是邮件写作助手。根据用户的指令写一封完整的邮件。'
    '只输出一个 JSON 对象，格式为 {"subject": "...", "body": "..."}：'
    'subject 是简明恰当的邮件主题；body 是完整的邮件正文，用换行分段，'
    '语气礼貌得体，语言跟随指令（未指明时用简体中文），'
    '开头有合适的称呼、结尾有落款。不要输出 JSON 以外的任何文字。'
)
EMAIL_PATTERN = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
MAX_INSTRUCTION_CHARS = 8_000
MAX_RECIPIENT_CHARS = 320
MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 50_000
PENDING_DRAFT_TTL_SECONDS = 15 * 60
MAX_PENDING_DRAFTS = 16
PENDING_DRAFTS: dict[str, dict[str, Any]] = {}
_PENDING_LOCK = threading.Lock()


class AssistantError(Exception):
    """Raised when the assistant cannot complete an operation."""


def extract_recipient(instruction: str) -> str:
    match = EMAIL_PATTERN.search(str(instruction or ''))
    return match.group(0) if match else ''


def validate_recipient(value: str) -> str:
    recipient = str(value or '').strip()
    if not recipient:
        raise AssistantError('收件人不能为空')
    if len(recipient) > MAX_RECIPIENT_CHARS:
        raise AssistantError('收件人长度超过限制')
    if any(character in recipient for character in '\r\n'):
        raise AssistantError('收件人不能包含换行符')
    if not EMAIL_PATTERN.fullmatch(recipient) or '..' in recipient.split('@', 1)[1]:
        raise AssistantError('收件人必须是单个有效邮箱地址')
    return recipient


def validate_subject(value: str) -> str:
    subject = ' '.join(str(value or '').split())
    if not subject:
        raise AssistantError('主题不能为空')
    if len(subject) > MAX_SUBJECT_CHARS:
        raise AssistantError('主题长度超过限制')
    return subject


def validate_body(value: str) -> str:
    body = str(value or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not body:
        raise AssistantError('正文不能为空')
    if len(body) > MAX_BODY_CHARS:
        raise AssistantError('正文长度超过限制')
    if '\x00' in body:
        raise AssistantError('正文包含无效字符')
    return body


def validate_draft_fields(to: str, subject: str, body: str) -> tuple[str, str, str]:
    return validate_recipient(to), validate_subject(subject), validate_body(body)


def generate_draft_via_ai(
    instruction: str,
    api_key: str,
    *,
    model: str | None = None,
    transport: Any = None,
) -> dict[str, str]:
    model_name = _resolve_model(model)
    instruction = str(instruction or '').strip()
    if not instruction:
        raise AssistantError('请先输入写邮件的指令')
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise AssistantError('写邮件指令长度超过限制')
    post = transport or requests.post
    content = _chat_once(
        post,
        api_key,
        model_name,
        DRAFT_SYSTEM_PROMPT,
        str(instruction or '').strip(),
    )
    match = re.search(r'\{.*\}', content, re.DOTALL)
    parsed = None
    if match:
        try:
            import json

            parsed = json.loads(match.group(0))
        except ValueError:
            parsed = None
    if not isinstance(parsed, dict) or not str(parsed.get('subject') or '').strip():
        raise SummaryAPIError('AI 未返回有效的草稿')
    return {
        'subject': validate_subject(parsed.get('subject')),
        'body': validate_body(parsed.get('body')),
    }


def build_draft_message(
    from_addr: str, to: str, subject: str, body: str
) -> EmailMessage:
    to, subject, body = validate_draft_fields(to, subject, body)
    message = EmailMessage()
    message['From'] = from_addr
    message['To'] = to
    message['Subject'] = subject
    message['Date'] = email.utils.formatdate(localtime=True)
    message.set_content(body)
    return message


def find_drafts_folder(connection: Any) -> str | None:
    status, folders = connection.list()
    if status != 'OK' or not folders:
        return None
    fallback = None
    for item in folders:
        raw = (
            item.decode('utf-8', errors='replace')
            if isinstance(item, (bytes, bytearray))
            else str(item)
        )
        pieces = raw.split(')', 1)
        name = pieces[1].strip().split(' ', 1)[-1] if len(pieces) > 1 else ''
        name = name.strip('"')
        lowered = raw.casefold()
        if '\\drafts' in lowered or '\\draft"' in lowered:
            return name
        if 'draft' in name.casefold() or '草稿' in name:
            fallback = name
    return fallback


def stage_draft_imap(
    host: str,
    port: int,
    username: str,
    password: str,
    from_addr: str,
    to: str,
    subject: str,
    body: str,
    *,
    imap_factory: Any = None,
) -> str:
    """Append one draft and return its stable staging reference."""
    message = build_draft_message(from_addr, to, subject, body)
    context = ssl.create_default_context()
    factory = imap_factory or _default_imap_factory
    connection = factory(host, port, 30, context)
    try:
        status, _ = connection.login(username, password)
        if status != 'OK':
            raise AssistantError('IMAP 登录失败，请检查授权码')
        folder = find_drafts_folder(connection) or 'Drafts'
        imap_folder = f'"{folder}"' if ' ' in folder else folder
        status, data = connection.append(
            imap_folder,
            '(\\Draft)',
            imaplib.Time2Internaldate(time.time()),
            message.as_bytes(),
        )
        if status != 'OK':
            raise AssistantError(f'保存草稿失败：{data!r}')
        response = b''
        for item in data or []:
            response += item if isinstance(item, bytes) else str(item).encode()
        match = re.search(rb'APPENDUID\s+(\d+)\s+(\d+)', response)
        if not match:
            raise AssistantError('草稿未返回稳定 UID，已停止发送流程')
        uidvalidity, uid = match.group(1), match.group(2)
        status, _ = connection.select(imap_folder, readonly=True)
        if status != 'OK':
            raise AssistantError('无法只读校验刚保存的草稿')
        status, data = connection.uid('FETCH', uid, '(BODY.PEEK[])')
        if status != 'OK':
            raise AssistantError('无法读取刚保存的草稿')
        raw = b''
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                raw += item[1]
        if not raw:
            raise AssistantError('刚保存的草稿内容为空')
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        _validate_staged_message(parsed, to, subject, body)
        return {
            'folder': folder,
            'uidvalidity': uidvalidity.decode('ascii'),
            'uid': uid.decode('ascii'),
            'message_sha256': _message_sha256(raw),
            'message_bytes': raw,
        }
    finally:
        try:
            connection.logout()
        except Exception:
            pass


def save_draft_imap(
    host: str,
    port: int,
    username: str,
    password: str,
    from_addr: str,
    to: str,
    subject: str,
    body: str,
    *,
    imap_factory: Any = None,
) -> str:
    reference = stage_draft_imap(
        host, port, username, password, from_addr, to, subject, body,
        imap_factory=imap_factory,
    )
    return reference['folder']


def _validate_staged_message(
    message: EmailMessage,
    to: str,
    subject: str,
    body: str,
    *,
    expected_from: str | None = None,
) -> None:
    recipients = [
        address.strip().casefold()
        for _name, address in email.utils.getaddresses(
            message.get_all('To', [])
        )
        if address.strip()
    ]
    if recipients != [to.casefold()]:
        raise AssistantError('草稿收件人校验失败')
    if expected_from:
        from_addresses = [
            address.strip().casefold()
            for _name, address in email.utils.getaddresses(
                message.get_all('From', [])
            )
            if address.strip()
        ]
        if from_addresses != [expected_from.casefold()]:
            raise AssistantError('草稿发件人校验失败')
    if str(message.get('Subject') or '') != subject:
        raise AssistantError('草稿主题校验失败')
    try:
        content = message.get_content()
    except Exception as error:
        raise AssistantError('草稿正文不可解析') from error
    if validate_body(content) != validate_body(body):
        raise AssistantError('草稿正文校验失败')


def _message_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _purge_pending_drafts_locked(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    expired = [
        pending_id
        for pending_id, context in PENDING_DRAFTS.items()
        if context.get('_expires_at_mono', 0) <= now
    ]
    for pending_id in expired:
        PENDING_DRAFTS.pop(pending_id, None)


def _store_pending_draft(
    context: dict[str, Any],
    *,
    now: float | None = None,
    ttl_seconds: int = PENDING_DRAFT_TTL_SECONDS,
    max_items: int = MAX_PENDING_DRAFTS,
) -> str:
    now = time.monotonic() if now is None else now
    with _PENDING_LOCK:
        _purge_pending_drafts_locked(now)
        while len(PENDING_DRAFTS) >= max_items:
            PENDING_DRAFTS.pop(next(iter(PENDING_DRAFTS)))
        while True:
            pending_id = secrets.token_urlsafe(32)
            if pending_id not in PENDING_DRAFTS:
                staged_context = dict(context)
                staged_context['_expires_at_mono'] = now + ttl_seconds
                PENDING_DRAFTS[pending_id] = staged_context
                return pending_id


def _take_pending_draft(pending_id: str) -> dict[str, Any]:
    with _PENDING_LOCK:
        _purge_pending_drafts_locked()
        try:
            return PENDING_DRAFTS.pop(str(pending_id))
        except KeyError as error:
            raise AssistantError('待发送草稿不存在、已确认或服务已重启') from error


def _assistant_graph_token(scopes: str) -> str:
    try:
        payload = refresh_master_graph_token(scopes)
    except MailboxFlowError as error:
        raise AssistantError(str(error)) from error
    return str(payload['access_token'])


def create_master_draft(access_token: str, to: str, subject: str, body: str) -> str:
    to, subject, body = validate_draft_fields(to, subject, body)
    response = requests.post(
        GRAPH_MESSAGES_URL,
        headers={'Authorization': f'Bearer {access_token}'},
        json={
            'subject': subject,
            'body': {'contentType': 'Text', 'content': body},
            'toRecipients': [{'emailAddress': {'address': to}}],
        },
        timeout=30,
    )
    if response.status_code in (401, 403):
        raise AssistantError(
            '学校要求管理员批准此应用的邮件写入权限。'
            '可复制正文到 Outlook 网页版手动发送，'
            '或联系学校 IT 授权"Microsoft Graph Command Line Tools"后重试。'
        )
    if response.status_code != 201:
        raise AssistantError(f'保存草稿失败：HTTP {response.status_code}')
    return str(response.json().get('id') or '')


def send_master_message(access_token: str, draft_id: str) -> None:
    response = requests.post(
        f'{GRAPH_MESSAGES_URL}/{draft_id}/send',
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=30,
    )
    if response.status_code in (401, 403):
        raise AssistantError(
            '学校要求管理员批准此应用的邮件发送权限。'
            '请联系学校 IT 授权"Microsoft Graph Command Line Tools"后重试。'
        )
    if response.status_code not in (200, 202):
        raise AssistantError(f'发送失败：HTTP {response.status_code}')


def verify_master_mailbox(access_token: str, expected_mailbox: str) -> None:
    expected = validate_recipient(expected_mailbox)
    response = requests.get(
        f'{GRAPH_ME_URL}?$select=mail,userPrincipalName',
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=30,
    )
    if response.status_code != 200:
        raise AssistantError(f'无法校验 Outlook 身份：HTTP {response.status_code}')
    profile = response.json()
    actual_values = {
        str(profile.get('mail') or '').strip().casefold(),
        str(profile.get('userPrincipalName') or '').strip().casefold(),
    }
    if expected.casefold() not in actual_values:
        raise AssistantError('Outlook 身份与配置的 master_mail 不匹配')


def verify_master_staged_draft(
    access_token: str,
    draft_id: str,
    to: str,
    subject: str,
    body: str,
) -> None:
    to, subject, body = validate_draft_fields(to, subject, body)
    response = requests.get(
        (
            f'{GRAPH_MESSAGES_URL}/{quote(draft_id, safe="")}'
            '?$select=subject,toRecipients,body,isDraft'
        ),
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=30,
    )
    if response.status_code != 200:
        raise AssistantError(f'无法读取待发送草稿：HTTP {response.status_code}')
    payload = response.json()
    if payload.get('isDraft') is not True:
        raise AssistantError('待发送消息不再是草稿')
    recipients = [
        str(
            ((item or {}).get('emailAddress') or {}).get('address') or ''
        ).strip().casefold()
        for item in payload.get('toRecipients') or []
    ]
    if recipients != [to.casefold()]:
        raise AssistantError('待发送草稿收件人校验失败')
    if validate_subject(payload.get('subject')) != subject:
        raise AssistantError('待发送草稿主题校验失败')
    graph_body = (payload.get('body') or {}).get('content')
    if validate_body(graph_body) != body:
        raise AssistantError('待发送草稿正文校验失败')


def fetch_staged_draft_imap(
    account: dict[str, str],
    context: dict[str, Any],
    *,
    imap_factory: Any = None,
) -> EmailMessage:
    context = dict(context)
    to, subject, body = validate_draft_fields(
        context.get('to'), context.get('subject'), context.get('body')
    )
    context_ssl = ssl.create_default_context()
    factory = imap_factory or _default_imap_factory
    connection = factory(
        account['host'], int(account['port']), 30, context_ssl
    )
    try:
        status, _ = connection.login(account['username'], account['password'])
        if status != 'OK':
            raise AssistantError('IMAP 登录失败，无法读取待发送草稿')
        folder = str(context.get('folder') or '')
        imap_folder = f'"{folder}"' if ' ' in folder else folder
        status, mailbox_data = connection.select(imap_folder, readonly=True)
        if status != 'OK':
            raise AssistantError('无法只读访问待发送草稿')
        mailbox_response = b''
        for item in mailbox_data or []:
            mailbox_response += item if isinstance(item, bytes) else str(item).encode()
        validity = re.search(rb'UIDVALIDITY\s+(\d+)', mailbox_response)
        if not validity or validity.group(1).decode() != str(
            context.get('uidvalidity')
        ):
            raise AssistantError('草稿文件夹状态已变化，请重新保存')
        status, data = connection.uid(
            'FETCH',
            str(context.get('uid')).encode('ascii'),
            '(FLAGS BODY.PEEK[])',
        )
        if status != 'OK':
            raise AssistantError('无法读取待发送草稿')
        raw = b''
        metadata = b''
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                metadata += item[0] if isinstance(item[0], bytes) else str(item[0]).encode()
                raw += item[1]
        if hashlib.sha256(raw).hexdigest() != context.get('message_sha256'):
            raise AssistantError('待发送草稿内容校验失败')
        message = BytesParser(policy=policy.default).parsebytes(raw)
        _validate_staged_message(message, to, subject, body)
        if b'\\draft' not in metadata.lower().replace(b'"', b''):
            raise AssistantError('待发送消息不再是草稿')
        return message
    finally:
        try:
            connection.logout()
        except Exception:
            pass


def send_existing_email_smtp(
    account: dict[str, str], message: EmailMessage
) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        account['host'],
        int(account['port']),
        timeout=30,
        context=context,
    ) as server:
        server.login(account['username'], account['password'])
        server.send_message(message)


def send_mail_smtp(
    host: str,
    port: int,
    username: str,
    password: str,
    from_addr: str,
    to: str,
    subject: str,
    body: str,
) -> None:
    message = build_draft_message(from_addr, to, subject, body)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as server:
        server.login(username, password)
        server.send_message(message)


def ai_generate_draft(instruction: str) -> dict[str, str]:
    ensure_environment()
    api_key = load_summary_api_key()
    if not api_key:
        raise AssistantError('未配置 GLM 密钥，无法使用 AI 写邮件')
    if not str(instruction or '').strip():
        raise AssistantError('请先输入写邮件的指令')
    return generate_draft_via_ai(instruction, api_key)


def _assistant_account(mailbox_id: str, credential_username: str) -> dict[str, str]:
    from windows_gui.imap_mail import (
        BachelorImapConfig,
        QqImapConfig,
    )
    from windows_gui.mail_backends import WindowsCredentialManagerSecretStore
    if mailbox_id == 'qq_mail':
        config = QqImapConfig.from_environment()
        store = WindowsCredentialManagerSecretStore(
            ASSISTANT_CREDENTIAL_SERVICE, credential_username
        )
        host, port = QQ_IMAP_HOST, QQ_IMAP_PORT
    elif mailbox_id == 'bachelor_mail':
        config = BachelorImapConfig.from_environment()
        store = WindowsCredentialManagerSecretStore(
            ASSISTANT_CREDENTIAL_SERVICE, credential_username
        )
        host, port = BACHELOR_IMAP_HOST, BACHELOR_IMAP_PORT
    else:
        raise AssistantError(f'不支持的邮箱：{mailbox_id}')
    password = store.get_secret()
    if not config.username or not password:
        raise AssistantError('邮箱账号或授权码未配置')
    return {
        'username': config.username,
        'password': password,
        'host': host,
        'port': str(port),
    }


def save_draft_for_mailbox(
    mailbox_id: str, to: str, subject: str, body: str
) -> str:
    ensure_environment()
    to, subject, body = validate_draft_fields(to, subject, body)
    if mailbox_id == 'master_mail':
        mailbox = os.environ.get('AI_WORK_OUTLOOK_MAILBOX', '').strip()
        token = _assistant_graph_token(ASSISTANT_SAVE_GRAPH_SCOPE)
        verify_master_mailbox(token, mailbox)
        create_master_draft(token, to, subject, body)
        return '已保存到 Outlook 草稿箱'
    credential_username = ASSISTANT_DRAFT_CREDENTIAL_USERNAMES.get(mailbox_id)
    if not credential_username:
        raise AssistantError(f'该邮箱不支持助手保存草稿：{mailbox_id}')
    account = _assistant_account(mailbox_id, credential_username)
    folder = save_draft_imap(
        account['host'],
        int(account['port']),
        account['username'],
        account['password'],
        account['username'],
        to,
        subject,
        body,
    )
    return f'已保存到草稿箱（{folder}）'


def stage_draft_for_mailbox(
    mailbox_id: str, to: str, subject: str, body: str
) -> dict[str, str]:
    ensure_environment()
    to, subject, body = validate_draft_fields(to, subject, body)
    if mailbox_id == 'master_mail':
        mailbox = os.environ.get('AI_WORK_OUTLOOK_MAILBOX', '').strip()
        token = _assistant_graph_token(ASSISTANT_SAVE_GRAPH_SCOPE)
        verify_master_mailbox(token, mailbox)
        draft_id = create_master_draft(token, to, subject, body)
        context = {
            'mailbox_id': mailbox_id,
            'draft_id': draft_id,
            'to': to,
            'subject': subject,
            'body': body,
        }
        detail = 'Outlook 草稿已保存，请再次确认发送'
    elif mailbox_id in ('qq_mail', 'bachelor_mail'):
        credential_username = ASSISTANT_DRAFT_CREDENTIAL_USERNAMES[mailbox_id]
        account = _assistant_account(mailbox_id, credential_username)
        reference = stage_draft_imap(
            account['host'],
            int(account['port']),
            account['username'],
            account['password'],
            account['username'],
            to,
            subject,
            body,
        )
        context = {
            'mailbox_id': mailbox_id,
            'draft_credential_username': credential_username,
            'from_addr': account['username'],
            'folder': reference['folder'],
            'uidvalidity': reference['uidvalidity'],
            'uid': reference['uid'],
            'message_sha256': reference['message_sha256'],
            'to': to,
            'subject': subject,
            'body': body,
        }
        detail = f"草稿已保存到 {reference['folder']}，请再次确认发送"
    else:
        raise AssistantError(f'不支持的邮箱：{mailbox_id}')
    pending_id = _store_pending_draft(context)
    return {'pending_id': pending_id, 'mailbox_id': mailbox_id, 'detail': detail}


def send_staged_draft(pending_id: str) -> str:
    context = _take_pending_draft(pending_id)
    mailbox_id = str(context.get('mailbox_id') or '')
    to, subject, body = validate_draft_fields(
        context.get('to'), context.get('subject'), context.get('body')
    )
    if mailbox_id == 'qq_mail':
        raise AssistantError('QQ 邮箱按安全规则不允许发送')
    if mailbox_id == 'master_mail':
        expected_mailbox = os.environ.get('AI_WORK_OUTLOOK_MAILBOX', '').strip()
        token = _assistant_graph_token(ASSISTANT_SEND_GRAPH_SCOPE)
        verify_master_mailbox(token, expected_mailbox)
        verify_master_staged_draft(
            token,
            str(context.get('draft_id')),
            to,
            subject,
            body,
        )
        send_master_message(token, str(context.get('draft_id')))
        return '已确认发送 Outlook 草稿'
    if mailbox_id == 'bachelor_mail':
        draft_account = _assistant_account(
            mailbox_id,
            str(context.get('draft_credential_username')),
        )
        message = fetch_staged_draft_imap(draft_account, context)
        account = _assistant_account(
            'bachelor_mail', BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME
        )
        send_existing_email_smtp(account, message)
        return '已确认发送本科邮箱已保存草稿'
    raise AssistantError(f'不支持的邮箱：{mailbox_id}')


def build_assistant_page() -> str:
    from windows_gui.mail_assistant_page import ASSISTANT_PAGE_HTML
    return ASSISTANT_PAGE_HTML
