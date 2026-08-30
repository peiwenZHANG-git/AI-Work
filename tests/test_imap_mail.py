"""Side-effect-free tests for the QQ Mail read-only IMAP backend."""

import imaplib
import ssl
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from windows_gui.imap_mail import (
    BACHELOR_IMAP_CREDENTIAL_SERVICE,
    BACHELOR_IMAP_CREDENTIAL_USERNAME,
    BACHELOR_IMAP_HOST,
    BACHELOR_IMAP_PORT,
    BACHELOR_IMAP_USERNAME_ENVIRONMENT,
    BachelorImapConfig,
    BachelorImapReadonlyBackend,
    ReadonlyMailboxMessage,
    QqImapConfig,
    QqImapReadonlyBackend,
    fetch_messages_readonly,
)
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


def _bachelor_backend(connection=None, factory=None, username='student@example.edu'):
    if factory is None:
        factory = lambda host, port, timeout, context: connection
    return BachelorImapReadonlyBackend(
        config=BachelorImapConfig(username=username),
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
        self.assertEqual(
            'QQ IMAP 只读检查完成；使用 EXAMINE、UID 和 BODY.PEEK，未改变已读状态',
            result.message,
        )
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


class BachelorImapReadonlyBackendTests(unittest.TestCase):
    @patch('windows_gui.mail_backends.keyring.get_password')
    def test_dedicated_credential_manager_entry_is_separate_from_qq(
        self, get_password
    ):
        get_password.return_value = 'stored-secret'
        store = WindowsCredentialManagerSecretStore(
            BACHELOR_IMAP_CREDENTIAL_SERVICE,
            BACHELOR_IMAP_CREDENTIAL_USERNAME,
        )
        self.assertEqual('stored-secret', store.get_secret())
        get_password.assert_called_once_with(
            'AI-Work/windows-gui/mailboxes',
            'bachelor_mail_imap_authorization_code',
        )
        self.assertNotEqual(
            'bachelor_mail_imap_authorization_code',
            'qq_mail_imap_authorization_code',
        )

    @patch.dict('os.environ', {}, clear=True)
    def test_missing_configuration_is_explicit(self):
        config = BachelorImapConfig.from_environment()
        self.assertEqual(BACHELOR_IMAP_USERNAME_ENVIRONMENT, config.username_environment)
        result = _bachelor_backend(username=None).summarize_today(10)
        self.assertIs(BackendStatus.IMAP_NOT_CONFIGURED, result.status)

    def test_success_uses_confirmed_host_tls_readonly_uid_and_body_peek(self):
        connection = FakeImapConnection(
            fetch_responses={b'10': _fetch_response()}
        )
        captured = {}

        def factory(host, port, timeout, context):
            captured.update(
                host=host,
                port=port,
                verify_mode=context.verify_mode,
                check_hostname=context.check_hostname,
            )
            return connection

        result = _bachelor_backend(connection, factory).summarize_today(10)
        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual(BACHELOR_IMAP_HOST, captured['host'])
        self.assertEqual(BACHELOR_IMAP_PORT, captured['port'])
        self.assertEqual(ssl.CERT_REQUIRED, captured['verify_mode'])
        self.assertTrue(captured['check_hostname'])
        self.assertEqual(('select', 'INBOX', True), connection.calls[1])
        uid_calls = [call for call in connection.calls if call[0] == 'uid']
        self.assertIn('BODY.PEEK[HEADER.FIELDS', uid_calls[1][3])
        for forbidden in ('STORE', 'MOVE', 'COPY', 'EXPUNGE'):
            self.assertNotIn(forbidden, repr(connection.calls).upper())
        self.assertFalse(result.emails[0].read_state_changed)
        self.assertEqual('IMAP_HEADER_METADATA', result.emails[0].summary_source)

    def test_authorization_failure_is_explicit(self):
        connection = FakeImapConnection()
        connection.login = lambda username, authorization_code: (_ for _ in ()).throw(
            imaplib.IMAP4.error('authentication failed')
        )
        result = _bachelor_backend(connection).summarize_today(10)
        self.assertIs(BackendStatus.IMAP_AUTH_FAILED, result.status)

    def test_network_failure_is_explicit(self):
        def unavailable(host, port, timeout, context):
            raise OSError('network unavailable')

        result = _bachelor_backend(factory=unavailable).summarize_today(10)
        self.assertIs(BackendStatus.IMAP_NETWORK_FAILED, result.status)

    def test_tls_failure_is_explicit(self):
        def tls_failure(host, port, timeout, context):
            raise ssl.SSLError('certificate verification failed')

        result = _bachelor_backend(factory=tls_failure).summarize_today(10)
        self.assertIs(BackendStatus.IMAP_NETWORK_FAILED, result.status)

    def test_protocol_failure_after_login_is_explicit(self):
        connection = FakeImapConnection()
        connection.select = lambda mailbox, readonly=False: (_ for _ in ()).throw(
            imaplib.IMAP4.error('EXAMINE rejected')
        )
        result = _bachelor_backend(connection).summarize_today(10)
        self.assertIs(BackendStatus.IMAP_PROTOCOL_ERROR, result.status)

    def test_today_header_metadata_is_parsed(self):
        connection = FakeImapConnection(fetch_responses={
            b'10': _fetch_response(
                subject='=?UTF-8?B?5pys56eR6YKu5Lu2?=',
                sender='School <sender@example.edu>',
            ),
        })
        result = _bachelor_backend(connection).summarize_today(10)
        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual('本科邮件', result.emails[0].subject)
        self.assertRegex(
            result.emails[0].message_reference or '', r'^imap:[0-9a-f]{24}$'
        )

    def test_empty_uid_search_is_confirmed_empty_today(self):
        result = _bachelor_backend(
            FakeImapConnection(search_uids=b'')
        ).summarize_today(10)
        self.assertIs(BackendStatus.EMPTY_TODAY, result.status)


class FetchMessagesFakeConnection:
    """Fake IMAP connection shaped for ``fetch_messages_readonly``."""

    def __init__(self, search_uids=b'10 11', sizes=None, bodies=None,
                 internal_dates=None):
        self.search_uids = search_uids
        self.sizes = sizes if sizes is not None else {}
        self.bodies = bodies if bodies is not None else {}
        self.internal_dates = internal_dates or {}
        self.calls = []

    def login(self, username, password):
        self.calls.append(('login', username, password))
        return 'OK', [b'authenticated']

    def select(self, mailbox, readonly=False):
        self.calls.append(('select', mailbox, readonly))
        return 'OK', [b'1']

    def uid(self, command, *args):
        self.calls.append(('uid', command, *args))
        if command == 'SEARCH':
            return 'OK', [self.search_uids]
        uid, fetch_item = args[0], args[1]
        if fetch_item == '(RFC822.SIZE)':
            if uid not in self.sizes:
                return 'NO', [b'size unavailable']
            size = self.sizes[uid]
            if size is None:
                metadata = f'1 (UID {uid.decode()})'.encode('ascii')
            else:
                metadata = (
                    f'1 (UID {uid.decode()} RFC822.SIZE {size})'
                ).encode('ascii')
            return 'OK', [(metadata, b''), b')']
        if fetch_item == '(INTERNALDATE BODY.PEEK[])':
            body = self.bodies.get(uid)
            if body is None:
                return 'NO', [b'body unavailable']
            internal_date = self.internal_dates.get(uid)
            metadata = f'1 (UID {uid.decode()} '.encode('ascii')
            if internal_date is not None:
                metadata += f'INTERNALDATE "{internal_date}" '.encode('ascii')
            metadata += f'BODY[] {{{len(body)}}}'.encode('ascii')
            return 'OK', [(metadata, body), b')']
        return 'NO', [b'unknown fetch item']

    def logout(self):
        self.calls.append(('logout',))
        return 'BYE', [b'logout']


def _raw_message(subject, sender='Sender <sender@example.com>'):
    return (
        f'From: {sender}\r\n'
        'To: me@example.com\r\n'
        f'Subject: {subject}\r\n'
        'Date: Thu, 27 Aug 2026 09:30:00 +0200\r\n'
        '\r\n'
        'Body line.\r\n'
    ).encode('ascii')


def _fetch_factory(connection, captured=None):
    def factory(host, port, timeout, context):
        if captured is not None:
            captured.update(
                host=host,
                port=port,
                timeout=timeout,
                verify_mode=context.verify_mode,
                check_hostname=context.check_hostname,
            )
        return connection

    return factory


class FetchMessagesReadonlyTests(unittest.TestCase):
    def _fetch(self, connection, captured=None, **kwargs):
        return fetch_messages_readonly(
            _fetch_factory(connection, captured),
            'imap.example.com',
            993,
            'user@example.com',
            'runtime-secret',
            since_date=TODAY,
            **kwargs,
        )

    def test_normal_read_returns_parsed_messages_newest_first(self):
        connection = FetchMessagesFakeConnection(
            search_uids=b'10 11',
            sizes={b'10': 500, b'11': 500},
            bodies={
                b'10': _raw_message('First subject'),
                b'11': _raw_message('Second subject'),
            },
            internal_dates={
                b'10': '27-Aug-2026 09:30:00 +0200',
                b'11': '27-Aug-2026 10:15:00 +0200',
            },
        )
        messages = self._fetch(connection, limit=10)

        self.assertEqual([b'11', b'10'], [item.uid for item in messages])
        self.assertEqual('Second subject', messages[0].message['Subject'])
        self.assertEqual('First subject', messages[1].message['Subject'])
        self.assertIn('Body line.', messages[0].message.get_content())
        self.assertEqual(('login', 'user@example.com', 'runtime-secret'),
                         connection.calls[0])
        self.assertEqual(('select', 'INBOX', True), connection.calls[1])
        self.assertEqual(
            ('uid', 'SEARCH', None, 'SINCE', '27-Aug-2026'),
            connection.calls[2],
        )
        self.assertEqual(('logout',), connection.calls[-1])

    def test_uses_ssl_default_context_body_peek_and_no_mutating_commands(self):
        captured = {}
        connection = FetchMessagesFakeConnection(
            search_uids=b'10',
            sizes={b'10': 500},
            bodies={b'10': _raw_message('Only subject')},
            internal_dates={b'10': '27-Aug-2026 09:30:00 +0200'},
        )
        messages = self._fetch(connection, captured=captured, limit=5)

        self.assertEqual('imap.example.com', captured['host'])
        self.assertEqual(993, captured['port'])
        self.assertEqual(ssl.CERT_REQUIRED, captured['verify_mode'])
        self.assertTrue(captured['check_hostname'])
        self.assertEqual(1, len(messages))
        body_fetches = [
            call for call in connection.calls
            if call[:3] == ('uid', 'FETCH', b'10')
            and 'BODY.PEEK' in call[3]
        ]
        self.assertEqual(1, len(body_fetches))
        self.assertEqual('(INTERNALDATE BODY.PEEK[])', body_fetches[0][3])
        size_probes = [
            call for call in connection.calls
            if call[:3] == ('uid', 'FETCH', b'10')
            and call[3] == '(RFC822.SIZE)'
        ]
        self.assertEqual(1, len(size_probes))
        serialized_calls = repr(connection.calls).upper()
        for forbidden in ('STORE', 'MOVE', 'COPY', 'EXPUNGE'):
            self.assertNotIn(forbidden, serialized_calls)

    def test_size_limit_skips_oversized_message_without_body_fetch(self):
        connection = FetchMessagesFakeConnection(
            search_uids=b'10 11',
            sizes={b'10': 400_000, b'11': 400_001},
            bodies={b'10': _raw_message('Small subject')},
            internal_dates={b'10': '27-Aug-2026 09:30:00 +0200'},
        )
        messages = self._fetch(connection)

        self.assertEqual([b'10'], [item.uid for item in messages])
        body_fetch_uids = [
            call[2] for call in connection.calls
            if call[:2] == ('uid', 'FETCH') and 'BODY.PEEK' in call[3]
        ]
        self.assertEqual([b'10'], body_fetch_uids)

    def test_message_exactly_at_size_limit_is_kept(self):
        connection = FetchMessagesFakeConnection(
            search_uids=b'10',
            sizes={b'10': 10},
            bodies={b'10': _raw_message('Tiny subject')},
            internal_dates={b'10': '27-Aug-2026 09:30:00 +0200'},
        )
        messages = self._fetch(connection, max_message_bytes=10)
        self.assertEqual([b'10'], [item.uid for item in messages])

    def test_unparseable_size_falls_through_to_body_fetch(self):
        connection = FetchMessagesFakeConnection(
            search_uids=b'10',
            sizes={b'10': None},
            bodies={b'10': _raw_message('No size subject')},
            internal_dates={b'10': '27-Aug-2026 09:30:00 +0200'},
        )
        messages = self._fetch(connection)
        self.assertEqual([b'10'], [item.uid for item in messages])

    def test_size_fetch_failure_skips_uid(self):
        connection = FetchMessagesFakeConnection(
            search_uids=b'10 11',
            sizes={b'11': 500},
            bodies={b'11': _raw_message('Surviving subject')},
            internal_dates={b'11': '27-Aug-2026 09:30:00 +0200'},
        )
        messages = self._fetch(connection)
        self.assertEqual([b'11'], [item.uid for item in messages])

    def test_internal_date_is_parsed_and_missing_date_reports_none(self):
        connection = FetchMessagesFakeConnection(
            search_uids=b'10 11',
            sizes={b'10': 500, b'11': 500},
            bodies={
                b'10': _raw_message('Dated subject'),
                b'11': _raw_message('Undated subject'),
            },
            internal_dates={
                b'10': '27-Aug-2026 09:30:00 +0200',
                b'11': None,
            },
        )
        messages = self._fetch(connection)

        by_uid = {item.uid: item for item in messages}
        dated = by_uid[b'10'].received
        self.assertIsNotNone(dated)
        self.assertEqual(TODAY, dated.date())
        self.assertEqual(9, dated.hour)
        self.assertEqual(30, dated.minute)
        self.assertIsNone(by_uid[b'11'].received)

    def test_missing_body_is_skipped(self):
        connection = FetchMessagesFakeConnection(
            search_uids=b'10',
            sizes={b'10': 500},
            bodies={},
        )
        messages = self._fetch(connection)
        self.assertEqual([], messages)

    def test_login_rejection_raises_and_still_logs_out(self):
        connection = FetchMessagesFakeConnection()
        connection.login = lambda username, password: ('NO', [b'bad credentials'])

        with self.assertRaises(imaplib.IMAP4.error):
            self._fetch(connection)
        self.assertEqual(('logout',), connection.calls[-1])

    def test_select_failure_raises_and_still_logs_out(self):
        connection = FetchMessagesFakeConnection()
        connection.select = lambda mailbox, readonly=False: ('NO', [b'no'])

        with self.assertRaises(imaplib.IMAP4.error):
            self._fetch(connection)
        self.assertEqual(('logout',), connection.calls[-1])

    def test_search_failure_raises_and_still_logs_out(self):
        connection = FetchMessagesFakeConnection()
        original_uid = connection.uid

        def failing_search(command, *args):
            if command == 'SEARCH':
                return 'NO', [b'search failed']
            return original_uid(command, *args)

        connection.uid = failing_search
        with self.assertRaises(imaplib.IMAP4.error):
            self._fetch(connection)
        self.assertEqual(('logout',), connection.calls[-1])

    def test_transport_error_during_fetch_propagates_and_logs_out(self):
        connection = FetchMessagesFakeConnection(
            search_uids=b'10',
            sizes={b'10': 500},
            bodies={b'10': _raw_message('Doomed subject')},
            internal_dates={b'10': '27-Aug-2026 09:30:00 +0200'},
        )
        original_uid = connection.uid

        def failing_body_fetch(command, *args):
            if command == 'FETCH' and 'BODY.PEEK' in args[1]:
                raise OSError('connection dropped')
            return original_uid(command, *args)

        connection.uid = failing_body_fetch
        with self.assertRaises(OSError):
            self._fetch(connection)
        self.assertEqual(('logout',), connection.calls[-1])

    def test_factory_failure_propagates_without_connection(self):
        def unavailable(host, port, timeout, context):
            raise OSError('network unavailable')

        with self.assertRaises(OSError):
            fetch_messages_readonly(
                unavailable,
                'imap.example.com',
                993,
                'user@example.com',
                'runtime-secret',
                since_date=TODAY,
            )


if __name__ == '__main__':
    unittest.main()
