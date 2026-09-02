"""Local web app for the AI mail assistant (127.0.0.1 only)."""

import json
import argparse
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from windows_gui.mail_assistant import (
    AssistantError,
    ai_generate_draft,
    build_assistant_page,
    generate_reply_draft,
    save_draft_for_mailbox,
    send_staged_draft,
    stage_draft_for_mailbox,
)
from windows_gui.mail_digest import DIGEST_DIR
from windows_gui.mail_digest import build_today_action_items
from windows_gui.mail_digest import dismiss_mail_keys
from windows_gui.mail_digest import dismissed_keys
from windows_gui.mail_digest import filter_dismissed_html
from windows_gui.mail_digest import remove_dismissed_from_latest_digest
from windows_gui.mail_digest import run_digest_update
from windows_gui.mail_search import natural_language_mail_search
from windows_gui.health_events import record_health_event
from windows_gui.system_health import collect_dashboard_health


PORT = 8931
MAX_JSON_BODY_BYTES = 256 * 1024
REFRESH_STATE = {'running': False, 'last_finished': None, 'last_ok': None}
_REFRESH_LOCK = threading.Lock()


def is_local_request(host: str | None, origin: str | None = None) -> bool:
    """Accept browser traffic only from the explicitly bound local origin."""
    if (host or '').casefold() != f'127.0.0.1:{PORT}':
        return False
    if not origin:
        return True
    normalized = origin.rstrip('/').casefold()
    return normalized in {
        f'http://127.0.0.1:{PORT}',
        f'http://localhost:{PORT}',
    }


def is_json_request(content_type: str | None) -> bool:
    """Require a CORS preflight before mutating assistant requests."""
    media_type = (content_type or '').split(';', 1)[0].strip().casefold()
    return media_type == 'application/json'


def parse_content_length(
    value: str | None, max_bytes: int = MAX_JSON_BODY_BYTES
) -> tuple[int, str | None]:
    """Parse a bounded Content-Length without allowing unbounded reads."""
    try:
        length = int((value or '0').strip())
    except ValueError:
        return 0, 'invalid_content_length'
    if length < 0:
        return 0, 'invalid_content_length'
    if length > max_bytes:
        return 0, 'content_too_large'
    return length, None


def _refresh_worker() -> None:
    try:
        result = run_digest_update(with_toasts=True)
        REFRESH_STATE['last_finished'] = result['generated_at']
        REFRESH_STATE['last_ok'] = result['ok']
    except Exception as error:  # keep the server alive on surprises
        REFRESH_STATE['last_finished'] = None
        REFRESH_STATE['last_ok'] = False
        print(f'refresh failed: {type(error).__name__}', file=sys.stderr)
    finally:
        REFRESH_STATE['running'] = False


def start_refresh() -> bool:
    with _REFRESH_LOCK:
        if REFRESH_STATE['running']:
            return False
        REFRESH_STATE['running'] = True
    threading.Thread(target=_refresh_worker, daemon=True).start()
    return True


class MailAssistantHandler(BaseHTTPRequestHandler):
    server_version = 'MailAssistant/1.0'

    def _send_html(self, html: str, code: int = 200) -> None:
        data = html.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header(
            'Content-Security-Policy',
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; "
            "form-action 'self'",
        )
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict, code: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - http.server API
        path = urlparse(self.path).path
        if not is_local_request(self.headers.get('Host'), self.headers.get('Origin')):
            self._send_json({'error': 'request origin is not local'}, 403)
            return
        if path == '/':
            self._send_html(build_assistant_page())
            return
        if path == '/digest':
            files = sorted(DIGEST_DIR.glob('*.html'))
            if not files:
                self._send_html('<h1>还没有摘要，请先运行一次更新</h1>', 404)
                return
            html = files[-1].read_text(encoding='utf-8')
            self._send_html(filter_dismissed_html(html, dismissed_keys()))
            return
        if path == '/api/status':
            self._send_json({'status': 'ok'})
            return
        if path == '/api/health':
            try:
                report = collect_dashboard_health(assistant_running=True)
            except Exception:
                self._send_json({'error': 'health_unavailable'}, 500)
                return
            self._send_json(report)
            return
        if path == '/api/refresh-status':
            self._send_json({
                'running': REFRESH_STATE['running'],
                'last_finished': REFRESH_STATE['last_finished'],
                'last_ok': REFRESH_STATE['last_ok'],
            })
            return
        if path == '/api/today-todos':
            files = sorted(DIGEST_DIR.glob('*.html'))
            if not files:
                self._send_json({'item_count': 0, 'items': []}, 404)
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int(query.get('limit', ['8'])[0])
                report = build_today_action_items(
                    files[-1].read_text(encoding='utf-8'),
                    limit=limit,
                )
            except (OSError, ValueError):
                self._send_json({'error': 'todo_request_invalid'}, 400)
                return
            self._send_json(report)
            return
        if path == '/api/stats':
            stats_file = DIGEST_DIR / 'last-run.json'
            try:
                data = json.loads(stats_file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                self._send_json({'generated_at': None, 'mailboxes': []})
                return
            names = {
                'qq_mail': 'QQ 邮箱',
                'bachelor_mail': '传媒大学',
                'master_mail': '巴黎萨克雷',
            }
            mailboxes = [
                {
                    'id': item.get('mailbox_id'),
                    'name': names.get(item.get('mailbox_id'), item.get('mailbox_id')),
                    'count': item.get('count', 0),
                    'ok': item.get('status') in ('READY', 'EMPTY_TODAY'),
                }
                for item in data.get('mailboxes', [])
            ]
            self._send_json({'generated_at': data.get('generated_at'), 'mailboxes': mailboxes})
            return
        self._send_html('<h1>404</h1>', 404)

    def do_POST(self):  # noqa: N802 - http.server API
        path = urlparse(self.path).path
        if not is_local_request(self.headers.get('Host'), self.headers.get('Origin')):
            self._send_json({'error': 'request origin is not local'}, 403)
            return
        if not is_json_request(self.headers.get('Content-Type')):
            self._send_json({'error': 'request must be application/json'}, 415)
            return
        length, length_error = parse_content_length(
            self.headers.get('Content-Length')
        )
        if length_error:
            self._send_json(
                {'error': length_error},
                413 if length_error == 'content_too_large' else 400,
            )
            return
        try:
            payload = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            self._send_json({'error': '请求不是有效的 JSON'}, 400)
            return
        handlers = {
            '/api/refresh': self._handle_refresh,
            '/api/ai-draft': self._handle_ai_draft,
            '/api/ai-reply-draft': self._handle_ai_reply_draft,
            '/api/save-draft': self._handle_save_draft,
            '/api/mail-search': self._handle_mail_search,
            '/api/stage-draft': self._handle_stage_draft,
            '/api/dismiss': self._handle_dismiss,
            '/api/send-mail': self._handle_send_mail,
        }
        handler = handlers.get(path)
        if handler is None:
            self._send_json({'error': 'unknown endpoint'}, 404)
            return
        try:
            handler(payload)
        except AssistantError as error:
            self._send_json({'error': str(error)}, 400)
        except Exception as error:  # keep the server alive on surprises
            print(f'request failed: {type(error).__name__}', file=sys.stderr)
            record_health_event(
                'mail_assistant', 'error', 'assistant_request_failed'
            )
            self._send_json({'error': 'internal_server_error'}, 500)

    def _handle_ai_draft(self, payload: dict) -> None:
        draft = ai_generate_draft(str(payload.get('instruction') or ''))
        self._send_json(draft)

    def _handle_ai_reply_draft(self, payload: dict) -> None:
        requested_mailbox = str(payload.get('mailbox_id') or 'master_mail')
        if requested_mailbox not in {
            'master_mail', 'bachelor_mail', 'qq_mail',
        }:
            raise AssistantError('不支持的邮箱')
        draft = generate_reply_draft(
            str(payload.get('key') or ''),
            str(payload.get('instruction') or ''),
        )
        draft['mailbox_id'] = requested_mailbox
        self._send_json(draft)

    def _handle_mail_search(self, payload: dict) -> None:
        limit = payload.get('limit', 20)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise AssistantError('搜索数量必须是 1 到 50 的整数')
        try:
            result = natural_language_mail_search(
                str(payload.get('query') or ''),
                max_results=limit,
            )
        except ValueError as error:
            raise AssistantError(str(error))
        self._send_json(result)

    def _handle_refresh(self, payload: dict) -> None:
        started = start_refresh()
        self._send_json({
            'started': started,
            'running': REFRESH_STATE['running'],
        })

    def _handle_save_draft(self, payload: dict) -> None:
        detail = save_draft_for_mailbox(
            str(payload.get('mailbox_id') or ''),
            str(payload.get('to') or ''),
            str(payload.get('subject') or ''),
            str(payload.get('body') or ''),
        )
        self._send_json({'detail': detail})

    def _handle_stage_draft(self, payload: dict) -> None:
        result = stage_draft_for_mailbox(
            str(payload.get('mailbox_id') or ''),
            str(payload.get('to') or ''),
            str(payload.get('subject') or ''),
            str(payload.get('body') or ''),
        )
        self._send_json(result)

    def _handle_dismiss(self, payload: dict) -> None:
        keys = payload.get('keys')
        if not isinstance(keys, list):
            self._send_json({'error': 'keys must be a list'}, 400)
            return
        clean_keys = [str(key) for key in keys]
        dismissed = dismiss_mail_keys(clean_keys)
        remove_dismissed_from_latest_digest(set(clean_keys))
        self._send_json({'dismissed': dismissed})

    def _handle_send_mail(self, payload: dict) -> None:
        detail = send_staged_draft(str(payload.get('pending_id') or ''))
        self._send_json({'detail': detail})

    def log_message(self, format, *args):  # silence default stderr noise
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--open', action='store_true', help='open the local page')
    parser.add_argument(
        '--no-refresh',
        action='store_true',
        help='start without reading mailboxes (used for safe restarts)',
    )
    args = parser.parse_args(argv)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', PORT), MailAssistantHandler)
    except OSError:
        webbrowser.open(f'http://127.0.0.1:{PORT}/')
        return 0
    print(f'mail assistant listening on http://127.0.0.1:{PORT}/')
    if not args.no_refresh:
        start_refresh()
    if args.open:
        webbrowser.open(f'http://127.0.0.1:{PORT}/')
    server.serve_forever()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
