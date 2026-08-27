"""Side-effect-free tests for the QQ Mail read-only IMAP backend."""

import imaplib
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from windows_gui.imap_mail import QqImapConfig, QqImapReadonlyBackend
from windows_gui.mail_backends import (
    BackendStatus,
    WindowsCredentialManagerSecretStore,
)


TODAY = date(2026, 8, 27)


class FakeImapConnection:
    def __init__(self, search_uids=b'10', fetch_responses=None):
        self.search_uids = search_uids
        self.fetch_responses = fetch_responses or {}
        self.calls = []

    def login(self, username, authorization_code):
        self.calls.append(('login', username, authorization_code))
        return 'OK', [b'authenticated']

    def select(self, mailbox, readonly=False):
        self.calls.append(('select', mailbox, readonly))
        return 'OK', [b'1']

    def uid(self, command, *args):
        self.calls.append(('uid', command, *args))
        if command == 'SEARCH':
            return 'OK', [self.search_uids]
        uid = args[0]
        return 'OK', self.fetch_responses[uid]

    def logout(self):
        self.calls.append(('logout',))
        return 'BYE', [b'logout']


def _fetch_response(
    subject='Test subject',
    sender='Sender <sender@example.com>',
    internal_date='27-Aug-2026 09:30:00 +0200',
):
    headers = f'From: {sender}\r\nSubject: {subject}\r\n\r\n'.encode('ascii')
    metadata = (
        f'1 (UID 10 INTERNALDATE "{internal_date}" '
        f'BODY[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] {{{len(headers)}}}'
    ).encode('ascii')
    return [(metadata, headers), b')']


def _backend(connection=None, factory=None):
    if factory is None:
        factory = lambda host, port, timeout, context: connection
    return QqImapReadonlyBackend(
        config=QqImapConfig(username='configured@example.com'),
        secret_store=SimpleNamespace(get_secret=lambda: 'runtime-authorization-code'),
        imap_factory=factory,
        today_factory=lambda: TODAY,
    )


class QqImapReadonlyBackendTests(unittest.TestCase):
    @patch('windows_gui.mail_backends.keyring.get_password')
    def test_dedicated_credential_manager_entry(self, get_password):
        get_password.return_value = 'stored-secret'
        store = WindowsCredentialManagerSecretStore(
            'AI-Work/windows-gui/mailboxes',
            'qq_mail_imap_authorization_code',
        )
        self.assertEqual('stored-secret', store.get_secret())
        get_password.assert_called_once_with(
            'AI-Work/windows-gui/mailboxes',
            'qq_mail_imap_authorization_code',
        )

    def test_missing_username_is_not_configured(self):
        backend = QqImapReadonlyBackend(
            config=QqImapConfig(username=None),
            secret_store=SimpleNamespace(get_secret=lambda: 'unused'),
        )
        result = backend.summarize_today(10)
        self.assertIs(BackendStatus.IMAP_NOT_CONFIGURED, result.status)

    def test_missing_credential_is_not_configured(self):
        backend = QqImapReadonlyBackend(
            config=QqImapConfig(username='configured@example.com'),
            secret_store=SimpleNamespace(get_secret=lambda: None),
        )
        result = backend.summarize_today(10)
        self.assertIs(BackendStatus.IMAP_NOT_CONFIGURED, result.status)

    def test_success_uses_ssl_readonly_uid_and_body_peek(self):
        connection = FakeImapConnection(
            fetch_responses={b'10': _fetch_response()}
        )
        result = _backend(connection).summarize_today(10)

        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual(1, len(result.emails))
        self.assertEqual(('select', 'INBOX', True), connection.calls[1])
        uid_calls = [call for call in connection.calls if call[0] == 'uid']
        self.assertEqual('SEARCH', uid_calls[0][1])
        self.assertEqual(('SEARCH', None, 'SINCE', '27-Aug-2026'), uid_calls[0][1:])
        self.assertEqual('FETCH', uid_calls[1][1])
        self.assertIn('BODY.PEEK[HEADER.FIELDS', uid_calls[1][3])
        serialized_calls = repr(connection.calls).upper()
        for forbidden in ('STORE', 'MOVE', 'COPY', 'EXPUNGE'):
            self.assertNotIn(forbidden, serialized_calls)
        self.assertFalse(result.emails[0].read_state_changed)
        self.assertRegex(
            result.emails[0].message_reference or '', r'^imap:[0-9a-f]{24}$'
        )

    def test_empty_uid_search_is_confirmed_empty_today(self):
        connection = FakeImapConnection(search_uids=b'')
        result = _backend(connection).summarize_today(10)
        self.assertIs(BackendStatus.EMPTY_TODAY, result.status)

    def test_parsed_older_row_is_empty_today_not_parse_failure(self):
        connection = FakeImapConnection(fetch_responses={
            b'10': _fetch_response(internal_date='26-Aug-2026 23:59:00 +0200'),
        })
        result = _backend(connection).summarize_today(10)
        self.assertIs(BackendStatus.EMPTY_TODAY, result.status)

    def test_authentication_failure_is_explicit(self):
        connection = FakeImapConnection()

        def reject(username, authorization_code):
            raise imaplib.IMAP4.error('authentication failed')

        connection.login = reject
        result = _backend(connection).summarize_today(10)
        self.assertIs(BackendStatus.IMAP_AUTH_FAILED, result.status)

    def test_network_failure_is_explicit(self):
        def unavailable(host, port, timeout, context):
            raise OSError('network unavailable')

        result = _backend(factory=unavailable).summarize_today(10)
        self.assertIs(BackendStatus.IMAP_NETWORK_FAILED, result.status)

    def test_encoded_sender_and_subject_are_decoded(self):
        connection = FakeImapConnection(fetch_responses={
            b'10': _fetch_response(
                subject='=?UTF-8?B?5rWL6K+V5Li76aKY?=',
                sender='=?UTF-8?B?5Y+R5Lu25Lq6?= <sender@example.com>',
            ),
        })
        result = _backend(connection).summarize_today(10)
        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual('测试主题', result.emails[0].subject)
        self.assertIn('发件人', result.emails[0].sender)

    def test_internal_date_is_used_as_received_time(self):
        connection = FakeImapConnection(fetch_responses={
            b'10': _fetch_response(internal_date='27-Aug-2026 23:59:00 +0200'),
        })
        result = _backend(connection).summarize_today(10)
        self.assertIs(BackendStatus.READY, result.status)
        self.assertIn('2026-08-27T', result.emails[0].time)

    def test_unparseable_candidates_are_not_reported_empty(self):
        connection = FakeImapConnection(fetch_responses={b'10': [(b'bad', b'')]})
        result = _backend(connection).summarize_today(10)
        self.assertIs(BackendStatus.MAIL_ITEMS_NOT_PARSED, result.status)


if __name__ == '__main__':
    unittest.main()
