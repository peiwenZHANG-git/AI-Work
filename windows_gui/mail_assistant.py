"""AI mail assistant: natural-language drafts, draft saving, and sending.

Drafts are saved only after they are generated and shown to the user; sending
always requires an explicit confirmation in the assistant page. QQ mailbox can
save drafts but can never send (project safety rule).
"""

from __future__ import annotations

import email.utils
import imaplib
import os
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
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


ASSISTANT_GRAPH_SCOPE = (
    'https://graph.microsoft.com/Mail.ReadWrite '
    'https://graph.microsoft.com/Mail.Send offline_access'
)
GRAPH_MESSAGES_URL = 'https://graph.microsoft.com/v1.0/me/messages'
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
    """Append the message to the mailbox Drafts folder; return folder name."""
    message = build_draft_message(from_addr, to, subject, body)
    context = ssl.create_default_context()
    factory = imap_factory or _default_imap_factory
    connection = factory(host, port, 30, context)
    try:
        status, _ = connection.login(username, password)
        if status != 'OK':
            raise AssistantError('IMAP 登录失败，请检查授权码')
        folder = find_drafts_folder(connection) or 'Drafts'
        if ' ' in folder:
            folder = f'"{folder}"'
        status, data = connection.append(
            folder,
            '(\\Draft)',
            imaplib.Time2Internaldate(time.time()),
            message.as_bytes(),
        )
        if status != 'OK':
            raise AssistantError(f'保存草稿失败：{data!r}')
        return folder
    finally:
        try:
            connection.logout()
        except Exception:
            pass


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
        token = _assistant_graph_token(ASSISTANT_GRAPH_SCOPE)
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


def send_mail_for_mailbox(
    mailbox_id: str, to: str, subject: str, body: str
) -> str:
    ensure_environment()
    to, subject, body = validate_draft_fields(to, subject, body)
    if mailbox_id == 'qq_mail':
        raise AssistantError('QQ 邮箱按安全规则不允许发送，请改用草稿箱')
    if mailbox_id == 'master_mail':
        mailbox = os.environ.get('AI_WORK_OUTLOOK_MAILBOX', '').strip()
        token = _assistant_graph_token(ASSISTANT_GRAPH_SCOPE)
        draft_id = create_master_draft(token, to, subject, body)
        send_master_message(token, draft_id)
        return '邮件已通过 Outlook 发送'
    if mailbox_id == 'bachelor_mail':
        account = _assistant_account(
            'bachelor_mail', BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME
        )
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
        host, port = SMTP_HOSTS['bachelor_mail']
        send_mail_smtp(
            host,
            port,
            account['username'],
            account['password'],
            account['username'],
            to,
            subject,
            body,
        )
        return f'草稿已保存到 {folder}，邮件已通过本科邮箱发送'
    raise AssistantError(f'不支持的邮箱：{mailbox_id}')


def build_assistant_page() -> str:
    from windows_gui.mail_assistant_page import ASSISTANT_PAGE_HTML
    return ASSISTANT_PAGE_HTML
