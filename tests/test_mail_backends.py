"""Side-effect-free tests for mailbox backend adapters."""

import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from windows_gui.mail_backends import (
    BackendEmail,
    BackendStatus,
    EdgeFallbackBackend,
    GraphBackendConfig,
    GraphReadonlyBackend,
    GRAPH_MESSAGES_ENDPOINT,
    GRAPH_SCOPES,
    MailBackendResult,
)
from windows_gui.mail_summary import _summarize_mailbox
from windows_gui.mailboxes import MAILBOX_IDENTITIES


def _configured_graph_backend(token_store, transport):
    return GraphReadonlyBackend(
        config=GraphBackendConfig(
            tenant_id='tenant',
            client_id='client',
            mailbox='mailbox',
        ),
        token_store=token_store,
        transport=transport,
    )


class GraphReadonlyBackendTests(unittest.TestCase):
    def test_graph_success_reads_only_requested_metadata(self):
        runtime_token = 'runtime-' + uuid.uuid4().hex
        token_store = SimpleNamespace(get_access_token=lambda: runtime_token)
        transport = MagicMock()
        received = datetime.now().astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        transport.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                'value': [{
                    'sender': {'emailAddress': {
                        'name': 'Alice', 'address': 'alice@example.com',
                    }},
                    'subject': 'Graph summary',
                    'receivedDateTime': received,
                }],
            },
        )
        backend = _configured_graph_backend(token_store, transport)

        result = backend.summarize_today(10)

        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual(1, len(result.emails))
        email = result.emails[0]
        self.assertEqual('Alice', email.sender)
        self.assertEqual('Graph summary', email.subject)
        self.assertNotIn(runtime_token, repr(result))
        request_url, headers, timeout = transport.call_args.args
        query = urlencode({
            '$top': 10,
            '$select': 'sender,subject,receivedDateTime',
            '$orderby': 'receivedDateTime desc',
        })
        self.assertEqual(f'{GRAPH_MESSAGES_ENDPOINT}?{query}', request_url)
        self.assertEqual(f'Bearer {runtime_token}', headers['Authorization'])
        self.assertEqual(10.0, timeout)

    def test_missing_oauth_configuration_is_not_authenticated(self):
        backend = GraphReadonlyBackend(
            config=GraphBackendConfig(),
            token_store=SimpleNamespace(get_access_token=lambda: None),
            transport=MagicMock(),
        )
        result = backend.summarize_today(10)
        self.assertIs(BackendStatus.NOT_AUTHENTICATED, result.status)
        backend.transport.assert_not_called()

    def test_missing_token_is_not_authenticated(self):
        backend = _configured_graph_backend(
            SimpleNamespace(get_access_token=lambda: None), MagicMock()
        )
        result = backend.summarize_today(10)
        self.assertIs(BackendStatus.NOT_AUTHENTICATED, result.status)
        backend.transport.assert_not_called()

    def test_expired_token_is_reported_without_edge_fallback(self):
        backend = _configured_graph_backend(
            SimpleNamespace(get_access_token=lambda: uuid.uuid4().hex),
            MagicMock(return_value=MagicMock(status_code=401)),
        )
        result = backend.summarize_today(10)
        self.assertIs(BackendStatus.TOKEN_EXPIRED, result.status)
        self.assertEqual([], list(result.emails))

    def test_graph_http_failure_is_request_failed(self):
        backend = _configured_graph_backend(
            SimpleNamespace(get_access_token=lambda: uuid.uuid4().hex),
            MagicMock(return_value=MagicMock(status_code=503)),
        )
        result = backend.summarize_today(10)
        self.assertIs(BackendStatus.REQUEST_FAILED, result.status)

    def test_graph_reads_only_minimum_scope(self):
        self.assertEqual(('Mail.Read',), GRAPH_SCOPES)



class _StaticBackend:
    def __init__(self, result):
        self.result = result

    def summarize_today(self, max_emails):
        return self.result


class BackendDispatchTests(unittest.TestCase):
    def test_outlook_graph_ready_does_not_call_edge(self):
        email = BackendEmail(
            sender='Alice',
            subject='Graph summary',
            time='10:00',
            summary='Graph metadata only',
        )
        backend = _StaticBackend(MailBackendResult(
            BackendStatus.READY, 'Graph ready', (email,),
        ))
        with (
            patch('windows_gui.mail_summary._backend_for_identity', return_value=backend),
            patch('windows_gui.mail_summary._summarize_with_edge') as edge,
        ):
            result = _summarize_mailbox(MAILBOX_IDENTITIES['master_mail'])
        edge.assert_not_called()
        self.assertEqual('READY', result['status'])
        self.assertEqual(1, result['today_count'])
        self.assertEqual(
            ['sender', 'subject', 'time', 'summary', 'summary_source', 'read_state_changed'],
            list(result['emails'][0]),
        )

    def test_graph_unauthenticated_maps_to_existing_not_ready_status(self):
        backend = _StaticBackend(MailBackendResult(
            BackendStatus.NOT_AUTHENTICATED, 'Graph login required',
        ))
        with patch(
            'windows_gui.mail_summary._backend_for_identity', return_value=backend
        ):
            result = _summarize_mailbox(MAILBOX_IDENTITIES['master_mail'])
        self.assertEqual('NOT_READY', result['status'])
        self.assertEqual(0, result['today_count'])
        self.assertEqual([], result['emails'])

    def test_graph_request_failure_maps_to_existing_error_status(self):
        backend = _StaticBackend(MailBackendResult(
            BackendStatus.REQUEST_FAILED, 'Graph request failed',
        ))
        with patch(
            'windows_gui.mail_summary._backend_for_identity', return_value=backend
        ):
            result = _summarize_mailbox(MAILBOX_IDENTITIES['master_mail'])
        self.assertEqual('ERROR', result['status'])
        self.assertEqual([], result['emails'])

    def test_qq_and_bachelor_use_existing_edge_fallback(self):
        expected = {'status': 'READY'}
        for mailbox_id in ('bachelor_mail', 'qq_mail'):
            with patch(
                'windows_gui.mail_summary._summarize_with_edge',
                return_value=expected,
            ) as edge:
                result = _summarize_mailbox(MAILBOX_IDENTITIES[mailbox_id])
            self.assertIs(expected, result)
            edge.assert_called_once()

    def test_edge_fallback_backend_preserves_existing_result(self):
        expected = {'status': 'NOT_READY'}
        backend = EdgeFallbackBackend(summarize=lambda: expected)
        result = backend.summarize_today(10)
        self.assertIs(expected, result.legacy_result)
        self.assertIs(BackendStatus.FALLBACK_REQUIRED, result.status)

    def test_mcp_outer_return_structure_remains_compatible(self):
        groups = []
        for identity in MAILBOX_IDENTITIES.values():
            group = {
                'mailbox_id': identity.mailbox_id,
                'display_name': identity.display_name,
                'status': 'READY',
                'message': 'read-only',
                'today_count': 0,
                'emails': [],
                'read_state_change': 'NONE',
            }
            groups.append(group)

        from windows_gui.mail_summary import summarize_all_mailboxes_today
        with patch(
            'windows_gui.mail_summary._summarize_mailbox', side_effect=groups
        ):
            result = summarize_all_mailboxes_today()

        self.assertEqual({'mailboxes', 'important_items'}, set(result))
        self.assertEqual(
            ['bachelor_mail', 'master_mail', 'qq_mail'],
            [group['mailbox_id'] for group in result['mailboxes']],
        )
        for group in result['mailboxes']:
            self.assertEqual(
                {
                    'mailbox_id', 'display_name', 'status', 'message', 'today_count',
                    'emails', 'read_state_change',
                },
                set(group),
            )


if __name__ == '__main__':
    unittest.main()
