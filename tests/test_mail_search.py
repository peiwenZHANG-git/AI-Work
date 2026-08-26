"""Side-effect-free tests for unified mailbox search."""

import unittest
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import windows_gui.mail_search as mail_search
from windows_gui.mail_backends import (
    BackendStatus,
    GraphBackendConfig,
    GraphReadonlyBackend,
    MailSearchEmail,
    MailSearchQuery,
    MailSearchResult,
)
from windows_gui.mailboxes import MAILBOX_IDENTITIES


class GraphSearchTests(unittest.TestCase):
    def test_graph_search_filters_metadata_and_returns_safe_id(self):
        runtime_token = 'runtime-' + uuid.uuid4().hex
        start = datetime.fromisoformat('2026-08-01T00:00:00+00:00')
        end = datetime.fromisoformat('2026-08-31T23:59:59+00:00')
        transport = MagicMock(return_value=SimpleNamespace(
            status_code=200,
            json=lambda: {
                'value': [{
                    'id': 'graph-message-id',
                    'sender': {'emailAddress': {
                        'name': 'Alice',
                        'address': 'alice@example.com',
                    }},
                    'subject': 'Quarterly invoice',
                    'receivedDateTime': '2026-08-26T10:30:00Z',
                }],
            },
        ))
        backend = GraphReadonlyBackend(
            config=GraphBackendConfig(
                tenant_id='tenant', client_id='client', mailbox='mailbox'
            ),
            token_store=SimpleNamespace(
                get_access_token=lambda: runtime_token
            ),
            transport=transport,
        )

        result = backend.search(MailSearchQuery(
            keyword='invoice',
            sender='alice',
            start_time=start,
            end_time=end,
            max_results=10,
        ))

        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual(1, len(result.emails))
        self.assertEqual('graph-message-id', result.emails[0].message_reference)
        self.assertEqual('GRAPH_MESSAGE_ID', result.emails[0].reference_kind)
        request_url = transport.call_args.args[0]
        query = parse_qs(urlparse(request_url).query)
        filter_text = query['$filter'][0]
        self.assertIn("contains(subject, 'invoice')", filter_text)
        self.assertIn("contains(sender/emailAddress/name, 'alice')", filter_text)
        self.assertIn('receivedDateTime ge 2026-08-01T00:00:00', filter_text)
        self.assertIn('receivedDateTime le 2026-08-31T23:59:59', filter_text)
        self.assertEqual(
            ['id,sender,subject,receivedDateTime'], query['$select']
        )
        self.assertEqual('eventual', transport.call_args.args[1]['ConsistencyLevel'])

    def test_graph_search_no_results_is_ready(self):
        backend = GraphReadonlyBackend(
            config=GraphBackendConfig(
                tenant_id='tenant', client_id='client', mailbox='mailbox'
            ),
            token_store=SimpleNamespace(
                get_access_token=lambda: uuid.uuid4().hex
            ),
            transport=lambda url, headers, timeout: SimpleNamespace(
                status_code=200, json=lambda: {'value': []},
            ),
        )
        result = backend.search(MailSearchQuery(max_results=10))
        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual((), result.emails)

    def test_graph_search_failure_is_reported_without_exception(self):
        backend = GraphReadonlyBackend(
            config=GraphBackendConfig(
                tenant_id='tenant', client_id='client', mailbox='mailbox'
            ),
            token_store=SimpleNamespace(
                get_access_token=lambda: uuid.uuid4().hex
            ),
            transport=lambda url, headers, timeout: SimpleNamespace(
                status_code=503,
            ),
        )
        result = backend.search(MailSearchQuery(max_results=10))
        self.assertIs(BackendStatus.REQUEST_FAILED, result.status)
        self.assertEqual((), result.emails)


class EdgeSearchTests(unittest.TestCase):
    @staticmethod
    def _snapshot():
        return {
            'controls': [
                {
                    'control_type': 'ListItem',
                    'name': 'Alice | Quarterly invoice | 2026-08-26 10:00',
                },
                {
                    'control_type': 'ListItem',
                    'name': 'Bob | Graph outage notice | 2026-08-25 09:00',
                },
                {
                    'control_type': 'ListItem',
                    'name': 'Carol | Unrelated message | 2026-08-24 08:00',
                },
            ],
        }

    def test_edge_search_filters_keyword_sender_and_time(self):
        query = MailSearchQuery(
            keyword='invoice',
            sender='alice',
            start_time=datetime.fromisoformat('2026-08-26T00:00:00+00:00'),
            end_time=datetime.fromisoformat('2026-08-26T23:59:59+00:00'),
            max_results=10,
        )
        with patch.object(
            mail_search,
            '_ensure_mailbox_page',
            return_value=(self._snapshot(), 'READY'),
        ):
            result = mail_search._search_with_edge(
                MAILBOX_IDENTITIES['qq_mail'], query
            )

        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual(1, len(result.emails))
        email = result.emails[0]
        self.assertEqual('Alice', email.sender)
        self.assertEqual('Quarterly invoice', email.subject)
        self.assertEqual('EDGE_METADATA_HASH', email.reference_kind)
        self.assertIn('edge:qq_mail:metadata:', email.message_reference)
        self.assertNotIn('sid', email.message_reference.casefold())

    def test_edge_search_no_results_is_ready(self):
        with patch.object(
            mail_search,
            '_ensure_mailbox_page',
            return_value=(self._snapshot(), 'READY'),
        ):
            result = mail_search._search_with_edge(
                MAILBOX_IDENTITIES['bachelor_mail'],
                MailSearchQuery(keyword='missing-token', max_results=10),
            )
        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual((), result.emails)


class SearchDispatchTests(unittest.TestCase):
    def test_outlook_graph_ready_does_not_call_edge(self):
        email = MailSearchEmail(
            sender='Alice',
            subject='Graph result',
            received_time=datetime.fromisoformat('2026-08-26T10:00:00+00:00'),
            message_reference='graph-message-id',
        )
        graph = SimpleNamespace(search=lambda query: MailSearchResult(
            BackendStatus.READY, 'Graph ready', (email,),
        ))
        with (
            patch.object(mail_search, '_graph_backend', return_value=graph),
            patch.object(mail_search, '_search_with_edge') as edge,
        ):
            result = mail_search._search_mailbox(
                MAILBOX_IDENTITIES['master_mail'],
                MailSearchQuery(max_results=10),
            )
        edge.assert_not_called()
        self.assertEqual('GRAPH_API', result['backend'])
        self.assertEqual('SERVER_METADATA', result['search_scope'])
        self.assertEqual(1, result['result_count'])

    def test_outlook_graph_failures_fall_back_to_edge(self):
        for status in (
            BackendStatus.NOT_AUTHENTICATED,
            BackendStatus.TOKEN_EXPIRED,
            BackendStatus.REQUEST_FAILED,
        ):
            graph = SimpleNamespace(search=lambda query: MailSearchResult(
                status, 'Graph unavailable',
            ))
            edge_email = MailSearchEmail(
                sender='Alice',
                subject='Edge result',
                received_time=datetime.fromisoformat(
                    '2026-08-26T10:00:00+00:00'
                ),
                message_reference='edge-message-reference',
                reference_kind='EDGE_METADATA_HASH',
            )
            with (
                patch.object(mail_search, '_graph_backend', return_value=graph),
                patch.object(
                    mail_search,
                    '_search_with_edge',
                    return_value=MailSearchResult(
                        BackendStatus.READY, 'Edge ready', (edge_email,),
                    ),
                ) as edge,
            ):
                result = mail_search._search_mailbox(
                    MAILBOX_IDENTITIES['master_mail'],
                    MailSearchQuery(max_results=10),
                )
            edge.assert_called_once()
            self.assertEqual('EDGE_GUI', result['backend'])
            self.assertEqual('READY', result['status'])
            self.assertEqual(1, result['result_count'])

    def test_qq_and_bachelor_remain_edge_backends(self):
        for mailbox_id in ('bachelor_mail', 'qq_mail'):
            edge_email = MailSearchEmail(
                sender='Alice',
                subject='Edge result',
                received_time=datetime.fromisoformat(
                    '2026-08-26T10:00:00+00:00'
                ),
                message_reference='edge-message-reference',
                reference_kind='EDGE_METADATA_HASH',
            )
            with patch.object(
                mail_search,
                '_search_with_edge',
                return_value=MailSearchResult(
                    BackendStatus.READY, 'Edge ready', (edge_email,),
                ),
            ) as edge:
                result = mail_search._search_mailbox(
                    MAILBOX_IDENTITIES[mailbox_id],
                    MailSearchQuery(max_results=10),
                )
            edge.assert_called_once()
            self.assertEqual('EDGE_GUI', result['backend'])
            self.assertEqual('VISIBLE_LIST_METADATA', result['search_scope'])

    def test_backend_failure_maps_to_error_group(self):
        graph = SimpleNamespace(search=lambda query: MailSearchResult(
            BackendStatus.REQUEST_FAILED, 'Graph failed',
        ))
        with (
            patch.object(mail_search, '_graph_backend', return_value=graph),
            patch.object(
                mail_search,
                '_search_with_edge',
                return_value=MailSearchResult(
                    BackendStatus.REQUEST_FAILED, 'Edge failed',
                ),
            ),
        ):
            result = mail_search._search_mailbox(
                MAILBOX_IDENTITIES['master_mail'],
                MailSearchQuery(max_results=10),
            )
        self.assertEqual('ERROR', result['status'])
        self.assertEqual([], result['results'])
        self.assertEqual('NONE', result['read_state_change'])

    def test_mailbox_filter_selects_only_requested_identity(self):
        groups = [{
            'mailbox_id': 'qq_mail',
            'display_name': 'QQ邮箱',
            'backend': 'EDGE_GUI',
            'search_scope': 'VISIBLE_LIST_METADATA',
            'status': 'READY',
            'message': 'Edge ready',
            'result_count': 0,
            'results': [],
            'read_state_change': 'NONE',
        }]
        with patch.object(
            mail_search, '_search_mailbox', return_value=groups[0]
        ) as dispatch:
            result = mail_search.search_mailboxes(
                mailbox_id='qq_mail', keyword='invoice', max_results=10,
            )
        dispatch.assert_called_once_with(
            MAILBOX_IDENTITIES['qq_mail'], dispatch.call_args.args[1]
        )
        self.assertEqual(['qq_mail'], [
            group['mailbox_id'] for group in result['mailboxes']
        ])
        self.assertEqual(0, result['result_count'])
        self.assertEqual('NONE', result['read_state_change'])

    def test_external_search_shape_has_no_body(self):
        email = MailSearchEmail(
            sender='Alice',
            subject='Result',
            received_time=datetime.fromisoformat('2026-08-26T10:00:00+00:00'),
            message_reference='safe-reference',
        )
        group = {
            'mailbox_id': 'master_mail',
            'display_name': '硕士邮箱',
            'backend': 'GRAPH_API',
            'search_scope': 'SERVER_METADATA',
            'status': 'READY',
            'message': 'Graph ready',
            'result_count': 1,
            'results': [email.as_result('master_mail')],
            'read_state_change': 'NONE',
        }
        with patch.object(
            mail_search, '_search_mailbox', return_value=group
        ):
            result = mail_search.search_mailboxes(
                keyword='result', max_results=10,
            )
        self.assertEqual(
            {
                'mailbox_id', 'sender', 'subject', 'received_time',
                'message_reference', 'reference_kind',
            },
            set(result['mailboxes'][0]['results'][0]),
        )
        self.assertEqual('NONE', result['read_state_change'])

    def test_invalid_time_and_limit_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ISO 8601'):
            mail_search.search_mailboxes(start_time='not-a-date')
        with self.assertRaisesRegex(ValueError, 'between 1 and 50'):
            mail_search.search_mailboxes(max_results=51)


if __name__ == '__main__':
    unittest.main()
