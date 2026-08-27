"""Side-effect-free tests for QQ and NetEase browser DOM summaries."""

import unittest
from datetime import date
from unittest.mock import Mock, patch

from windows_gui.browser_mail import (
    BrowserDomReadonlyBackend,
    BrowserMailboxConfig,
    parse_dom_mailbox_snapshot,
)
from windows_gui.mail_backends import BackendStatus


TODAY = date(2026, 8, 27)


def _config(mailbox_id: str = 'qq_mail') -> BrowserMailboxConfig:
    return BrowserMailboxConfig(
        mailbox_id=mailbox_id,
        profile_directory='Profile 3' if mailbox_id == 'qq_mail' else 'Profile 1',
        service_domains=('mail.qq.com', 'wx.mail.qq.com') if mailbox_id == 'qq_mail'
        else ('mailh.qiye.163.com',),
        endpoint_environment='TEST_CDP_ENDPOINT',
    )


class BrowserDomParserTests(unittest.TestCase):
    def _ready_snapshot(self):
        return {
            'list_found': True,
            'items': [{
                'sender': 'Sender',
                'subject': 'Subject',
                'received_time': '2026-08-27T09:30:00+02:00',
                'message_reference': 'provider-private-id',
            }],
        }

    def test_qq_dom_list_parser_returns_only_safe_metadata(self):
        result = parse_dom_mailbox_snapshot('qq_mail', self._ready_snapshot(), 10, TODAY)
        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual(1, len(result.emails))
        email = result.emails[0].as_result()
        self.assertEqual('Sender', email['sender'])
        self.assertEqual('Subject', email['subject'])
        self.assertEqual('BROWSER_DOM_METADATA', email['summary_source'])
        self.assertRegex(email['message_reference'], r'^dom:[0-9a-f]{24}$')
        self.assertNotIn('provider-private-id', str(email))

    def test_netease_dom_list_parser(self):
        result = parse_dom_mailbox_snapshot(
            'bachelor_mail', self._ready_snapshot(), 10, TODAY
        )
        self.assertIs(BackendStatus.READY, result.status)

    def test_explicit_empty_list_is_empty_today(self):
        result = parse_dom_mailbox_snapshot(
            'qq_mail',
            {'list_found': True, 'empty_state_found': True, 'items': []},
            10,
            TODAY,
        )
        self.assertIs(BackendStatus.EMPTY_TODAY, result.status)

    def test_login_expired_is_auth_required(self):
        result = parse_dom_mailbox_snapshot(
            'qq_mail', {'auth_required': True}, 10, TODAY
        )
        self.assertIs(BackendStatus.AUTH_REQUIRED, result.status)

    def test_selector_change_reports_list_not_found(self):
        result = parse_dom_mailbox_snapshot(
            'bachelor_mail', {'list_found': False, 'items': []}, 10, TODAY
        )
        self.assertIs(BackendStatus.MAIL_LIST_NOT_FOUND, result.status)

    def test_unparsed_rows_are_not_reported_as_empty(self):
        result = parse_dom_mailbox_snapshot(
            'qq_mail',
            {'list_found': True, 'items': [{'sender': '', 'subject': '', 'received_time': ''}]},
            10,
            TODAY,
        )
        self.assertIs(BackendStatus.MAIL_ITEMS_NOT_PARSED, result.status)


class BrowserDomBackendTests(unittest.TestCase):
    @patch.dict('os.environ', {}, clear=True)
    def test_missing_endpoint_is_backend_not_ready(self):
        reader = Mock()
        backend = BrowserDomReadonlyBackend(_config(), lambda: 'READY', reader)
        result = backend.summarize_today(10)
        self.assertIs(BackendStatus.BROWSER_BACKEND_NOT_READY, result.status)
        reader.assert_not_called()

    @patch.dict('os.environ', {'TEST_CDP_ENDPOINT': 'http://127.0.0.1:9222'})
    def test_attach_failure_is_explicit(self):
        reader = Mock(side_effect=TimeoutError('bounded attach timeout'))
        backend = BrowserDomReadonlyBackend(_config(), lambda: 'READY', reader)
        result = backend.summarize_today(10)
        self.assertIs(BackendStatus.BROWSER_ATTACH_FAILED, result.status)

    @patch.dict('os.environ', {'TEST_CDP_ENDPOINT': 'http://127.0.0.1:9222'})
    def test_identity_mismatch_stops_before_dom_read(self):
        reader = Mock()
        backend = BrowserDomReadonlyBackend(
            _config(), lambda: 'IDENTITY_MISMATCH', reader
        )
        result = backend.summarize_today(10)
        self.assertIs(BackendStatus.IDENTITY_MISMATCH, result.status)
        reader.assert_not_called()

    @patch.dict('os.environ', {'TEST_CDP_ENDPOINT': 'http://192.168.1.2:9222'})
    def test_non_loopback_debug_endpoint_is_rejected(self):
        reader = Mock()
        backend = BrowserDomReadonlyBackend(_config(), lambda: 'READY', reader)
        result = backend.summarize_today(10)
        self.assertIs(BackendStatus.BROWSER_BACKEND_NOT_READY, result.status)
        reader.assert_not_called()

    @patch.dict(
        'os.environ',
        {'TEST_CDP_ENDPOINT': 'ws://127.0.0.1:9222/devtools/browser/private-id'},
    )
    def test_websocket_session_url_is_rejected(self):
        reader = Mock()
        backend = BrowserDomReadonlyBackend(_config(), lambda: 'READY', reader)
        result = backend.summarize_today(10)
        self.assertIs(BackendStatus.BROWSER_BACKEND_NOT_READY, result.status)
        reader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
