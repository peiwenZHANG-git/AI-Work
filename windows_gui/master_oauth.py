"""One-time Microsoft authorization-code login for the master mailbox."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .mail_digest import (
    MailboxFlowError,
    write_master_refresh_token,
)


MASTER_LOGIN_SCOPE = (
    'offline_access https://graph.microsoft.com/Mail.Read '
    'https://graph.microsoft.com/Mail.ReadWrite '
    'https://graph.microsoft.com/Mail.Send'
)
DEFAULT_REDIRECT_PORT = 8932
_LOGIN_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class AuthorizationFlow:
    state: str
    code_verifier: str
    code_challenge: str
    redirect_uri: str


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return verifier, challenge


def build_authorization_url(
    tenant: str,
    client_id: str,
    *,
    port: int = DEFAULT_REDIRECT_PORT,
    state: str | None = None,
    code_verifier: str | None = None,
) -> tuple[str, AuthorizationFlow]:
    """Build the browser URL without emitting client or mail credentials."""
    if not tenant or not client_id:
        raise MailboxFlowError('Graph 租户或应用 ID 环境变量未配置')
    verifier, challenge = _pkce_pair() if code_verifier is None else (
        code_verifier,
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode('ascii')).digest()
        )
        .rstrip(b'=')
        .decode('ascii'),
    )
    flow = AuthorizationFlow(
        state=state or secrets.token_urlsafe(32),
        code_verifier=verifier,
        code_challenge=challenge,
        redirect_uri=f'http://127.0.0.1:{port}/callback',
    )
    tenant = str(tenant).strip()
    params = urlencode({
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': flow.redirect_uri,
        'scope': MASTER_LOGIN_SCOPE,
        'state': flow.state,
        'code_challenge': flow.code_challenge,
        'code_challenge_method': 'S256',
        'prompt': 'select_account',
    })
    return (
        f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?{params}',
        flow,
    )


def parse_callback(
    path: str,
    host: str | None,
    query: str,
    *,
    expected_state: str,
    expected_port: int,
) -> tuple[str, str, str]:
    """Validate a callback and return ``(kind, value, state)`` without logging it."""
    if path != '/callback':
        return ('not_found', 'not_found', '')
    if (host or '').casefold() != f'127.0.0.1:{expected_port}':
        return ('forbidden', 'forbidden', '')
    values = parse_qs(query, keep_blank_values=True)
    state = (values.get('state') or [''])[0]
    if not state or not secrets.compare_digest(state, expected_state):
        return ('state_mismatch', 'state_mismatch', state)
    error = (values.get('error') or [''])[0]
    if error:
        description = (values.get('error_description') or [''])[0]
        return ('error', description or error, state)
    code = (values.get('code') or [''])[0]
    if not code:
        return ('error', 'authorization code is missing', state)
    return ('code', code, state)


class _CallbackHTTPServer(HTTPServer):
    result: tuple[str, str, str] | None = None


def _response_handler_factory(expected_state: str, port: int):
    class CallbackHandler(BaseHTTPRequestHandler):
        server_version = 'AIMailLogin/1.0'

        def _respond(self, code: int, title: str, detail: str) -> None:
            body = (
                '<!doctype html><meta charset="utf-8">'
                f'<title>{title}</title><h1>{title}</h1><p>{detail}</p>'
            ).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header(
                'Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'"
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - http.server API
            parsed = urlparse(self.path)
            outcome = parse_callback(
                parsed.path,
                self.headers.get('Host'),
                parsed.query,
                expected_state=expected_state,
                expected_port=port,
            )
            if outcome[0] == 'code':
                self.server.result = outcome
                self._respond(200, '登录完成', '可以关闭此页面并返回终端。')
            elif outcome[0] == 'error':
                self.server.result = outcome
                self._respond(400, '登录失败', '请返回终端查看明确错误。')
            else:
                # Invalid paths, foreign Host values, and CSRF state mismatches
                # must not complete or cancel the pending authorization.
                self._respond(404, '请求无效', '此地址不能完成登录。')

        def log_message(self, format, *args):  # noqa: A002 - http.server API
            return

    return CallbackHandler


def _wait_for_authorization(
    server: _CallbackHTTPServer, timeout_seconds: float
) -> tuple[str, str, str]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while server.result is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MailboxFlowError('等待 OAuth 回调超时')
        server.timeout = remaining
        server.handle_request()
    return server.result


def exchange_authorization_code(
    tenant: str,
    client_id: str,
    code: str,
    flow: AuthorizationFlow,
    *,
    transport: Any = None,
) -> dict[str, Any]:
    """Exchange a one-time code, persist the rotated refresh token, and return status."""
    if not code or not flow.code_verifier:
        raise MailboxFlowError('OAuth 授权码或 PKCE 校验器缺失')
    post = transport or requests.post
    base = f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0'
    try:
        response = post(
            f'{base}/token',
            data={
                'grant_type': 'authorization_code',
                'client_id': client_id,
                'code': code,
                'redirect_uri': flow.redirect_uri,
                'code_verifier': flow.code_verifier,
                'scope': MASTER_LOGIN_SCOPE,
            },
            timeout=30,
        )
    except requests.RequestException as error:
        raise MailboxFlowError(f'OAuth token 网络失败：{type(error).__name__}') from error
    try:
        payload = response.json()
    except ValueError as error:
        raise MailboxFlowError('OAuth token 响应不是 JSON') from error
    if response.status_code != 200 or not payload.get('access_token'):
        raise MailboxFlowError(
            f"OAuth 登录失败：{payload.get('error', 'unknown_error')}"
        )
    refresh_token = payload.get('refresh_token')
    if not refresh_token:
        raise MailboxFlowError('OAuth 响应缺少 refresh token，未保存任何凭据')
    try:
        write_master_refresh_token(str(refresh_token))
    except (OSError, ValueError) as error:
        raise MailboxFlowError(f'OAuth 凭据写入失败：{type(error).__name__}') from error
    return {'stored_refresh_token': True}


def bootstrap_master_login(
    *,
    open_browser: bool = True,
    port: int = DEFAULT_REDIRECT_PORT,
    timeout_seconds: float = _LOGIN_TIMEOUT_SECONDS,
    transport: Any = None,
    browser_opener: Any = None,
    output: Any = None,
) -> dict[str, Any]:
    """Run the interactive login loop without printing or retaining token material."""
    tenant = os.environ.get('AI_WORK_OUTLOOK_TENANT_ID', '').strip()
    client_id = os.environ.get('AI_WORK_OUTLOOK_CLIENT_ID', '').strip()
    if not tenant or not client_id:
        raise MailboxFlowError('Graph 租户或应用 ID 环境变量未配置')
    authorization_url, flow = build_authorization_url(
        tenant, client_id, port=port
    )
    try:
        server = _CallbackHTTPServer(
            ('127.0.0.1', port), _response_handler_factory(flow.state, port)
        )
    except OSError as error:
        raise MailboxFlowError(f'OAuth 回调端口不可用：{port}') from error

    try:
        emit = output or print
        emit(f'打开下方 URL 完成 Outlook 登录（有效期 {int(timeout_seconds)} 秒）：')
        emit(authorization_url)
        if open_browser:
            opener = browser_opener or webbrowser.open
            try:
                opener(authorization_url)
            except Exception as error:
                emit(f'自动打开浏览器失败，请手动复制 URL：{type(error).__name__}')
        try:
            outcome = _wait_for_authorization(server, timeout_seconds)
        except MailboxFlowError:
            raise
    finally:
        server.server_close()

    kind, value, _state = outcome
    if kind != 'code':
        if kind == 'error':
            raise MailboxFlowError(f'OAuth 登录被拒绝：{value}')
        raise MailboxFlowError(f'OAuth 回调无效：{kind}')
    return exchange_authorization_code(
        tenant, client_id, value, flow, transport=transport
    )
