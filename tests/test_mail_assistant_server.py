"""Security and flow tests for the local mail assistant web server."""

from __future__ import annotations

import importlib.util
import io
import json
import unittest
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

import windows_gui.mail_assistant as mail_assistant


def _load_server_module():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'mail_assistant_server.py'
    spec = importlib.util.spec_from_file_location('test_mail_assistant_server_target', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_startup_module():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'start_mail_assistant.py'
    spec = importlib.util.spec_from_file_location('test_mail_assistant_startup_target', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MailAssistantServerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.server = _load_server_module()

    def test_json_content_type_is_required_for_post(self):
        self.assertTrue(self.server.is_json_request('application/json; charset=utf-8'))
        self.assertFalse(self.server.is_json_request('text/plain'))

    def test_refresh_start_is_single_flight(self):
        created = []

        class FakeThread:
            def __init__(self, target=None, daemon=None):
                created.append((target, daemon))

            def start(self):
                return None

        self.server.REFRESH_STATE['running'] = False
        with mock.patch.object(
            self.server.threading, 'Thread', FakeThread
        ):
            self.assertTrue(self.server.start_refresh())
            self.assertFalse(self.server.start_refresh())

        self.assertEqual(1, len(created))
        self.assertTrue(self.server.REFRESH_STATE['running'])
        self.server.REFRESH_STATE['running'] = False

    def test_html_response_has_security_headers(self):
        handler = object.__new__(self.server.MailAssistantHandler)
        handler.wfile = io.BytesIO()
        emitted = []
        handler.send_response = lambda code: emitted.append(('status', code))
        handler.send_header = lambda name, value: emitted.append((name, value))
        handler.end_headers = lambda: None

        handler._send_html('<html></html>', 200)

        headers = dict(emitted)
        self.assertEqual(200, headers['status'])
        self.assertEqual('nosniff', headers['X-Content-Type-Options'])
        self.assertEqual('no-referrer', headers['Referrer-Policy'])
        self.assertEqual('SAMEORIGIN', headers['X-Frame-Options'])
        self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
        self.assertIn("frame-ancestors 'self'", headers['Content-Security-Policy'])

    def test_content_length_is_validated_and_bounded(self):
        parse = self.server.parse_content_length
        self.assertEqual((0, None), parse(None))
        self.assertEqual((2, None), parse('2'))
        self.assertEqual((0, 'invalid_content_length'), parse('bad'))
        self.assertEqual((0, 'invalid_content_length'), parse('-1'))
        self.assertEqual(
            (0, 'content_too_large'), parse(str(self.server.MAX_JSON_BODY_BYTES + 1))
        )

    def test_post_rejects_invalid_and_oversized_bodies_before_reading(self):
        cases = [
            ('invalid_content_length', 'bad', 400),
            ('content_too_large', str(self.server.MAX_JSON_BODY_BYTES + 1), 413),
        ]
        for expected_error, header, expected_code in cases:
            with self.subTest(header=header):
                handler = object.__new__(self.server.MailAssistantHandler)
                handler.path = '/unknown'
                handler.headers = {
                    'Host': '127.0.0.1:8931',
                    'Content-Type': 'application/json',
                    'Content-Length': header,
                }
                handler.rfile = io.BytesIO(b'{"must_not_read":true}')
                responses = []
                handler._send_json = lambda payload, code=0, responses=responses, expected_code=expected_code: (
                    responses.append((payload, code))
                )

                handler.do_POST()

                self.assertEqual(
                    [( {'error': expected_error}, expected_code)], responses
                )
                self.assertEqual(0, handler.rfile.tell())

    def test_generic_failure_does_not_return_internal_details(self):
        handler = object.__new__(self.server.MailAssistantHandler)
        handler.path = '/api/ai-draft'
        handler.headers = {
            'Host': '127.0.0.1:8931',
            'Content-Type': 'application/json',
            'Content-Length': str(len(b'{"instruction":"private instruction"}')),
        }
        handler.rfile = io.BytesIO(b'{"instruction":"private instruction"}')
        responses = []
        handler._send_json = lambda payload, code=200: responses.append(
            (payload, code)
        )

        with mock.patch.object(
            self.server,
            'ai_generate_draft',
            side_effect=RuntimeError('private C:\\path and session detail'),
        ):
            handler.do_POST()

        self.assertEqual([({'error': 'internal_server_error'}, 500)], responses)

    def test_dismiss_endpoint_accepts_only_a_json_key_list(self):
        handler = object.__new__(self.server.MailAssistantHandler)
        handler.path = '/api/dismiss'
        handler.headers = {
            'Host': '127.0.0.1:8931',
            'Content-Type': 'application/json',
            'Content-Length': str(len(b'{"keys":["safe-hash"]}')),
        }
        handler.rfile = io.BytesIO(b'{"keys":["safe-hash"]}')
        responses = []
        handler._send_json = lambda payload, code=200: responses.append(
            (payload, code)
        )

        with mock.patch.object(
            self.server,
            'dismiss_mail_keys',
            return_value=1,
        ) as dismiss:
            handler.do_POST()

        self.assertEqual([({'dismissed': 1}, 200)], responses)
        dismiss.assert_called_once_with(['safe-hash'])

    def test_foreign_browser_origin_is_rejected(self):
        self.assertTrue(self.server.is_local_request('127.0.0.1:8931'))
        self.assertTrue(
            self.server.is_local_request(
                '127.0.0.1:8931', 'http://localhost:8931/'
            )
        )

    def test_startup_refresh_uses_required_json_media_type(self):
        captured = []

        def capture_urlopen(request, timeout=None):
            captured.append(request)

        startup = _load_startup_module()
        with mock.patch.object(startup.urllib.request, 'urlopen', capture_urlopen):
            startup.trigger_refresh()

        self.assertEqual(1, len(captured))
        self.assertEqual(b'{}', captured[0].data)
        self.assertEqual('application/json', captured[0].headers.get('Content-type'))
        self.assertFalse(self.server.is_local_request('attacker.example:8931'))
        self.assertFalse(
            self.server.is_local_request(
                '127.0.0.1:8931', 'https://attacker.example'
            )
        )


class MailAssistantSendFlowTests(unittest.TestCase):
    def setUp(self):
        self.server = _load_server_module()

    def test_stage_and_send_endpoints_are_separate(self):
        handler = object.__new__(self.server.MailAssistantHandler)
        responses = []
        handler._send_json = lambda payload, code=200: responses.append(
            (payload, code)
        )

        def post(path, payload):
            handler.path = path
            handler.headers = {
                'Host': '127.0.0.1:8931',
                'Content-Type': 'application/json',
                'Content-Length': str(len(payload)),
            }
            handler.rfile = io.BytesIO(payload)
            handler.do_POST()

        with mock.patch.object(
            self.server,
            'stage_draft_for_mailbox',
            return_value={'pending_id': 'pending-token', 'detail': 'staged'},
        ) as stage, mock.patch.object(
            self.server,
            'send_staged_draft',
            return_value='sent',
        ) as send:
            post(
                '/api/stage-draft',
                b'{"mailbox_id":"bachelor_mail","to":"teacher@example.edu",'
                b'"subject":"Subject","body":"Body"}',
            )
            post('/api/send-mail', b'{"pending_id":"pending-token"}')

        self.assertEqual([( {'pending_id': 'pending-token', 'detail': 'staged'}, 200)], responses[:1])
        self.assertEqual([({'detail': 'sent'}, 200)], responses[1:])
        stage.assert_called_once_with(
            'bachelor_mail',
            'teacher@example.edu',
            'Subject',
            'Body',
        )
        send.assert_called_once_with('pending-token')


class AssistantRestartTests(unittest.TestCase):
    def setUp(self):
        self.startup = _load_startup_module()

    def test_find_assistant_pids_matches_only_exact_server_script(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'mail_assistant_server.py'
            script.write_text('# server', encoding='utf-8')
            expected = str(script.resolve())
            payload = [
                {
                    'ProcessId': 101,
                    'CommandLine': f'"C:\\Python\\pythonw.exe" "{expected}"',
                },
                {
                    'ProcessId': 102,
                    'CommandLine': '"C:\\Python\\python.exe" other.py',
                },
                {
                    'ProcessId': 103,
                    'CommandLine': f'"C:\\Python\\python.exe" "{expected}" --no-refresh',
                },
            ]

            def runner(command, **kwargs):
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(payload), ''
                )

            pids = self.startup.find_assistant_pids(script, runner=runner)

        self.assertEqual([101, 103], pids)

    def test_restart_stops_exact_pids_and_starts_without_refresh(self):
        port_states = iter([True, True, False, False])
        launched = []
        stopped = []
        opened = []
        refreshed = []
        with mock.patch.object(
            self.startup, 'port_in_use', side_effect=lambda: next(port_states)
        ), mock.patch.object(
            self.startup,
            'find_assistant_pids',
            return_value=[123, 456],
        ) as find_pids, mock.patch.object(
            self.startup,
            'stop_assistant_pids',
            side_effect=lambda pids: stopped.extend(pids),
        ), mock.patch.object(
            self.startup,
            'launch_server',
            side_effect=lambda no_refresh: launched.append(no_refresh),
        ), mock.patch.object(
            self.startup, 'trigger_refresh', side_effect=refreshed.append
        ), mock.patch.object(
            self.startup.webbrowser, 'open', side_effect=opened.append
        ):
            exit_code = self.startup.main(['--restart', '--no-open'])

        self.assertEqual(0, exit_code)
        self.assertEqual([123, 456], stopped)
        self.assertEqual([True], launched)
        self.assertEqual([], refreshed)
        self.assertEqual([], opened)
        find_pids.assert_called_once()


if __name__ == '__main__':
    unittest.main()
