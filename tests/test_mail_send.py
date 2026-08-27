"""Side-effect-free tests for sending an existing mailbox draft."""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
import windows_gui.mail_send as mail_send
from windows_gui.mail_backends import (
    BackendStatus,
    GRAPH_MESSAGES_ENDPOINT,
    GRAPH_SEND_SCOPES,
    GraphBackendConfig,
    GraphReadonlyBackend,
    MailSendRequest,
    MailSendResult,
)
from windows_gui.mailboxes import MAILBOX_IDENTITIES


class GraphSendBackendTests(unittest.TestCase):
    @staticmethod
    def _response(status_code, payload=None):
        response = MagicMock(status_code=status_code)
        if payload is None:
            response.json.side_effect = ValueError('no JSON body')
        else:
            response.json.return_value = payload
        return response

    @staticmethod
    def _draft_payload():
        return {
            'id': 'graph-draft-id',
            'isDraft': True,
            'subject': 'Existing draft',
            'toRecipients': [{
                'emailAddress': {'address': 'alice@example.com'},
            }],
            'ccRecipients': [],
            'bccRecipients': [],
        }

    @staticmethod
    def _backend(transport, send_transport):
        return GraphReadonlyBackend(
            config=GraphBackendConfig(
                tenant_id='tenant',
                client_id='client',
                mailbox='master@example.com',
            ),
            token_store=SimpleNamespace(
                get_access_token=lambda: 'runtime-' + uuid.uuid4().hex,
            ),
            transport=transport,
            send_transport=send_transport,
        )

    def test_unconfirmed_request_is_rejected_before_transport(self):
        transport = MagicMock()
        send_transport = MagicMock()
        backend = self._backend(transport, send_transport)

        result = backend.send_draft(MailSendRequest(
            draft_reference='graph-draft-id', confirmed=False,
        ))

        self.assertIs(BackendStatus.NOT_CONFIRMED, result.status)
        self.assertFalse(result.send_attempted)
        transport.assert_not_called()
        send_transport.assert_not_called()

    def test_graph_sends_existing_draft_after_metadata_validation(self):
        transport = MagicMock(side_effect=[
            self._response(200, {
                'userPrincipalName': 'master@example.com',
            }),
            self._response(200, self._draft_payload()),
        ])
        send_transport = MagicMock(return_value=self._response(202))
        backend = self._backend(transport, send_transport)

        result = backend.send_draft(MailSendRequest(
            draft_reference='graph-draft-id', confirmed=True,
        ))

        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual('alice@example.com', result.recipient)
        self.assertEqual('Existing draft', result.subject)
        self.assertIsNone(result.sent_reference)
        self.assertIsNone(result.reference_kind)
        self.assertTrue(result.send_attempted)
        draft_url = transport.call_args_list[1].args[0]
        self.assertEqual(
            f'{GRAPH_MESSAGES_ENDPOINT}/graph-draft-id',
            draft_url.split('?')[0],
        )
        self.assertIn('%24select=', draft_url)
        self.assertNotIn('body', draft_url)
        self.assertEqual(
            f'{GRAPH_MESSAGES_ENDPOINT}/graph-draft-id/send',
            send_transport.call_args.args[0],
        )
        self.assertEqual(
            ('Mail.ReadWrite', 'Mail.Send'), GRAPH_SEND_SCOPES,
        )

    def test_missing_draft_is_reported_without_send(self):
        transport = MagicMock(side_effect=[
            self._response(200, {
                'userPrincipalName': 'master@example.com',
            }),
            self._response(404),
        ])
        send_transport = MagicMock()
        backend = self._backend(transport, send_transport)

        result = backend.send_draft(MailSendRequest(
            draft_reference='missing-draft', confirmed=True,
        ))

        self.assertIs(BackendStatus.DRAFT_NOT_FOUND, result.status)
        self.assertFalse(result.send_attempted)
        send_transport.assert_not_called()

    def test_graph_identity_mismatch_does_not_read_or_send_draft(self):
        transport = MagicMock(return_value=self._response(200, {
            'userPrincipalName': 'other@example.com',
        }))
        send_transport = MagicMock()
        backend = self._backend(transport, send_transport)

        result = backend.send_draft(MailSendRequest(
            draft_reference='graph-draft-id', confirmed=True,
        ))

        self.assertIs(BackendStatus.IDENTITY_MISMATCH, result.status)
        self.assertEqual(1, transport.call_count)
        self.assertFalse(result.send_attempted)
        send_transport.assert_not_called()

    def test_draft_owner_mismatch_is_rejected_before_send(self):
        payload = self._draft_payload()
        payload['from'] = {
            'emailAddress': {'address': 'other@example.com'},
        }
        transport = MagicMock(side_effect=[
            self._response(200, {
                'userPrincipalName': 'master@example.com',
            }),
            self._response(200, payload),
        ])
        send_transport = MagicMock()
        backend = self._backend(transport, send_transport)

        result = backend.send_draft(MailSendRequest(
            draft_reference='graph-draft-id', confirmed=True,
        ))

        self.assertIs(BackendStatus.IDENTITY_MISMATCH, result.status)
        self.assertEqual('alice@example.com', result.recipient)
        self.assertFalse(result.send_attempted)
        send_transport.assert_not_called()

    def test_multi_recipient_draft_is_invalid(self):
        payload = self._draft_payload()
        payload['toRecipients'].append({
            'emailAddress': {'address': 'bob@example.com'},
        })
        transport = MagicMock(side_effect=[
            self._response(200, {
                'userPrincipalName': 'master@example.com',
            }),
            self._response(200, payload),
        ])
        send_transport = MagicMock()
        backend = self._backend(transport, send_transport)

        result = backend.send_draft(MailSendRequest(
            draft_reference='graph-draft-id', confirmed=True,
        ))

        self.assertIs(BackendStatus.INVALID_DRAFT, result.status)
        self.assertFalse(result.send_attempted)
        send_transport.assert_not_called()

    def test_send_failure_is_reported_after_attempt(self):
        transport = MagicMock(side_effect=[
            self._response(200, {
                'userPrincipalName': 'master@example.com',
            }),
            self._response(200, self._draft_payload()),
        ])
        send_transport = MagicMock(return_value=self._response(500))
        backend = self._backend(transport, send_transport)

        result = backend.send_draft(MailSendRequest(
            draft_reference='graph-draft-id', confirmed=True,
        ))

        self.assertIs(BackendStatus.REQUEST_FAILED, result.status)
        self.assertTrue(result.send_attempted)
        self.assertEqual('alice@example.com', result.recipient)
        self.assertEqual('Existing draft', result.subject)

    def test_send_transport_exception_is_reported_after_attempt(self):
        transport = MagicMock(side_effect=[
            self._response(200, {
                'userPrincipalName': 'master@example.com',
            }),
            self._response(200, self._draft_payload()),
        ])
        backend = self._backend(
            transport,
            MagicMock(side_effect=requests.ConnectionError),
        )

        result = backend.send_draft(MailSendRequest(
            draft_reference='graph-draft-id', confirmed=True,
        ))

        self.assertIs(BackendStatus.REQUEST_FAILED, result.status)
        self.assertTrue(result.send_attempted)


class SendDispatchTests(unittest.TestCase):
    def test_tool_requires_explicit_confirmation(self):
        with patch.object(mail_send, '_send_existing_draft') as send:
            result = mail_send.send_mail_draft(
                'master_mail', 'graph-draft-id', False,
            )
        send.assert_not_called()
        self.assertEqual('CONFIRMATION_REQUIRED', result['status'])
        self.assertFalse(result['sent'])
        self.assertFalse(result['send_attempted'])

    def test_edge_reference_for_other_mailbox_is_rejected(self):
        with patch.object(mail_send, '_graph_backend') as backend:
            result = mail_send.send_mail_draft(
                'master_mail',
                'edge:qq_mail:draft:' + 'a' * 64,
                True,
            )
        backend.assert_not_called()
        self.assertEqual('DRAFT_MAILBOX_MISMATCH', result['status'])
        self.assertFalse(result['send_attempted'])

    def test_edge_draft_references_are_not_unsafely_sent(self):
        for mailbox_id in ('master_mail', 'bachelor_mail'):
            with patch.object(mail_send, '_graph_backend') as backend:
                result = mail_send.send_mail_draft(
                    mailbox_id,
                    f'edge:{mailbox_id}:draft:' + 'a' * 64,
                    True,
                )
            backend.assert_not_called()
            self.assertEqual('NOT_READY', result['status'])
            self.assertEqual('NONE', result['backend'])
            self.assertFalse(result['sent'])
            self.assertFalse(result['send_attempted'])

    def test_qq_remains_send_forbidden(self):
        with patch.object(mail_send, '_send_existing_draft') as send:
            result = mail_send.send_mail_draft(
                'qq_mail',
                'edge:qq_mail:draft:' + 'a' * 64,
                True,
            )
        send.assert_not_called()
        self.assertEqual('ERROR', result['status'])
        self.assertFalse(result['sent'])
        self.assertFalse(result['send_attempted'])

    def test_unconfigured_graph_backend_is_not_ready_without_fallback(self):
        backend = GraphReadonlyBackend(
            config=GraphBackendConfig(),
            token_store=SimpleNamespace(get_access_token=lambda: None),
        )
        with patch.object(mail_send, '_graph_backend', return_value=backend):
            result = mail_send.send_mail_draft(
                'master_mail', 'graph-draft-id', True,
            )
        self.assertEqual('NOT_READY', result['status'])
        self.assertEqual('GRAPH_API', result['backend'])
        self.assertFalse(result['sent'])
        self.assertFalse(result['send_attempted'])

    def test_external_return_shape_and_success_mapping(self):
        backend = SimpleNamespace(send_draft=lambda request: MailSendResult(
            BackendStatus.READY,
            'Draft sent',
            recipient='alice@example.com',
            subject='Existing draft',
            send_attempted=True,
        ))
        with patch.object(mail_send, '_graph_backend', return_value=backend):
            result = mail_send.send_mail_draft(
                'master_mail', 'graph-draft-id', True,
            )
        self.assertEqual({
            'mailbox_id', 'display_name', 'backend', 'status', 'message',
            'recipient', 'subject', 'sent_reference', 'reference_kind',
            'sent', 'send_attempted',
        }, set(result))
        self.assertEqual('READY', result['status'])
        self.assertEqual('alice@example.com', result['recipient'])
        self.assertEqual('Existing draft', result['subject'])
        self.assertTrue(result['sent'])
        self.assertTrue(result['send_attempted'])

    def test_unknown_mailbox_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown mailbox identity'):
            mail_send.send_mail_draft(
                'missing', 'graph-draft-id', True,
            )


if __name__ == '__main__':
    unittest.main()
