"""Side-effect-free tests for unified mailbox draft creation."""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
import windows_gui.mail_draft as mail_draft
import windows_gui.mail_send as mail_send
from windows_gui.mail_backends import (
    BackendStatus,
    GraphBackendConfig,
    GRAPH_DRAFT_SCOPES,
    GRAPH_ME_ENDPOINT,
    GRAPH_MESSAGES_ENDPOINT,
    GraphReadonlyBackend,
    MailDraftRequest,
    MailDraftResult,
)
from windows_gui.mailboxes import MAILBOX_IDENTITIES


class GraphDraftBackendTests(unittest.TestCase):
    @staticmethod
    def _backend(transport, draft_transport):
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
            draft_transport=draft_transport,
        )

    def test_graph_creates_draft_after_identity_verification(self):
        transport = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {'userPrincipalName': 'master@example.com'},
        ))
        draft_transport = MagicMock(return_value=MagicMock(
            status_code=201,
            json=lambda: {'id': 'graph-draft-id'},
        ))
        backend = self._backend(transport, draft_transport)
        request = MailDraftRequest(
            to='alice@example.com',
            subject='Draft subject',
            body='Draft body',
        )

        result = backend.create_draft(request)

        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual('graph-draft-id', result.draft_reference)
        self.assertEqual('GRAPH_DRAFT_ID', result.reference_kind)
        self.assertEqual(GRAPH_ME_ENDPOINT, transport.call_args.args[0])
        self.assertEqual(GRAPH_MESSAGES_ENDPOINT, draft_transport.call_args.args[0])
        payload = draft_transport.call_args.args[2]
        self.assertEqual({
            'subject': 'Draft subject',
            'body': {
                'contentType': 'Text',
                'content': 'Draft body',
            },
            'toRecipients': [{
                'emailAddress': {'address': 'alice@example.com'},
            }],
        }, payload)
        self.assertNotIn('/send', draft_transport.call_args.args[0])
        self.assertEqual(('Mail.ReadWrite',), GRAPH_DRAFT_SCOPES)

    def test_graph_identity_mismatch_does_not_create_draft(self):
        transport = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {'userPrincipalName': 'other@example.com'},
        ))
        draft_transport = MagicMock()
        backend = self._backend(transport, draft_transport)

        result = backend.create_draft(MailDraftRequest(
            to='alice@example.com', subject='Subject', body='Body',
        ))

        self.assertIs(BackendStatus.IDENTITY_MISMATCH, result.status)
        self.assertIsNone(result.draft_reference)
        draft_transport.assert_not_called()

    def test_graph_http_failure_is_reported(self):
        transport = MagicMock(return_value=MagicMock(status_code=503))
        draft_transport = MagicMock()
        backend = self._backend(transport, draft_transport)

        result = backend.create_draft(MailDraftRequest(
            to='alice@example.com', subject='Subject', body='Body',
        ))

        self.assertIs(BackendStatus.REQUEST_FAILED, result.status)
        draft_transport.assert_not_called()

    def test_graph_transport_exception_is_reported(self):
        backend = self._backend(
            MagicMock(side_effect=requests.ConnectionError), MagicMock(),
        )
        result = backend.create_draft(MailDraftRequest(
            to='alice@example.com', subject='Subject', body='Body',
        ))
        self.assertIs(BackendStatus.REQUEST_FAILED, result.status)

    def test_verified_graph_draft_requires_persisted_metadata(self):
        identity = MagicMock(
            status_code=200,
            json=lambda: {'userPrincipalName': 'master@example.com'},
        )
        created = MagicMock(status_code=201, json=lambda: {'id': 'draft-1'})
        verified = MagicMock(status_code=200, json=lambda: {
            'id': 'draft-1',
            'isDraft': True,
            'subject': 'Subject',
            'body': {'contentType': 'html', 'content': '<div>Body</div>'},
            'toRecipients': [{'emailAddress': {'address': 'alice@example.com'}}],
            'from': {'emailAddress': {'address': 'master@example.com'}},
        })
        transport = MagicMock(side_effect=[identity, verified])
        backend = GraphReadonlyBackend(
            config=GraphBackendConfig(
                tenant_id='tenant', client_id='client',
                mailbox='master@example.com', verify_created_draft=True,
            ),
            token_store=SimpleNamespace(get_access_token=lambda: 'runtime-token'),
            transport=transport,
            draft_transport=MagicMock(return_value=created),
        )
        result = backend.create_draft(MailDraftRequest(
            to='alice@example.com', subject='Subject', body='Body',
        ))
        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual('draft-1', result.draft_reference)
        self.assertEqual(2, transport.call_count)

    def test_verified_graph_draft_rejects_non_draft_or_mismatched_metadata(self):
        identity = MagicMock(
            status_code=200,
            json=lambda: {'userPrincipalName': 'master@example.com'},
        )
        created = MagicMock(status_code=201, json=lambda: {'id': 'draft-1'})
        verified = MagicMock(status_code=200, json=lambda: {
            'id': 'draft-1', 'isDraft': False, 'subject': 'Subject',
        })
        backend = GraphReadonlyBackend(
            config=GraphBackendConfig(
                tenant_id='tenant', client_id='client',
                mailbox='master@example.com', verify_created_draft=True,
            ),
            token_store=SimpleNamespace(get_access_token=lambda: 'runtime-token'),
            transport=MagicMock(side_effect=[identity, verified]),
            draft_transport=MagicMock(return_value=created),
        )
        result = backend.create_draft(MailDraftRequest(
            to='alice@example.com', subject='Subject', body='Body',
        ))
        self.assertIs(BackendStatus.INVALID_DRAFT, result.status)
        self.assertIsNone(result.draft_reference)
        self.assertEqual('draft-1', result.recovery_reference)

    def test_verified_graph_draft_accepts_omitted_owner_after_me_verification(self):
        identity = MagicMock(
            status_code=200,
            json=lambda: {'userPrincipalName': 'master@example.com'},
        )
        created = MagicMock(status_code=201, json=lambda: {'id': 'draft-1'})
        verified = MagicMock(status_code=200, json=lambda: {
            'id': 'draft-1', 'isDraft': True, 'subject': 'Subject',
            'body': {'contentType': 'html', 'content': '<div>Body</div>'},
            'toRecipients': [{'emailAddress': {'address': 'alice@example.com'}}],
        })
        backend = GraphReadonlyBackend(
            config=GraphBackendConfig(
                tenant_id='tenant', client_id='client',
                mailbox='master@example.com', verify_created_draft=True,
            ),
            token_store=SimpleNamespace(get_access_token=lambda: 'runtime-token'),
            transport=MagicMock(side_effect=[identity, verified]),
            draft_transport=MagicMock(return_value=created),
        )
        result = backend.create_draft(MailDraftRequest(
            to='alice@example.com', subject='Subject', body='Body',
        ))
        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual('draft-1', result.draft_reference)

    def test_verified_graph_draft_rejects_mismatched_owner_when_present(self):
        identity = MagicMock(
            status_code=200,
            json=lambda: {'userPrincipalName': 'master@example.com'},
        )
        created = MagicMock(status_code=201, json=lambda: {'id': 'draft-1'})
        verified = MagicMock(status_code=200, json=lambda: {
            'id': 'draft-1', 'isDraft': True, 'subject': 'Subject',
            'body': {'contentType': 'text', 'content': 'Body'},
            'toRecipients': [{'emailAddress': {'address': 'alice@example.com'}}],
            'sender': {'emailAddress': {'address': 'other@example.com'}},
        })
        backend = GraphReadonlyBackend(
            config=GraphBackendConfig(
                tenant_id='tenant', client_id='client',
                mailbox='master@example.com', verify_created_draft=True,
            ),
            token_store=SimpleNamespace(get_access_token=lambda: 'runtime-token'),
            transport=MagicMock(side_effect=[identity, verified]),
            draft_transport=MagicMock(return_value=created),
        )
        result = backend.create_draft(MailDraftRequest(
            to='alice@example.com', subject='Subject', body='Body',
        ))
        self.assertIs(BackendStatus.INVALID_DRAFT, result.status)
        self.assertIsNone(result.draft_reference)

    def test_master_graph_backend_refreshes_instead_of_stale_access_token(self):
        with patch.object(
            mail_draft, 'refresh_master_graph_token',
            return_value={'access_token': 'fresh-token'},
        ) as refresh:
            backend = mail_draft._graph_backend()
            token = backend.token_store.get_access_token()
        refresh.assert_called_once_with(
            'https://graph.microsoft.com/Mail.ReadWrite offline_access',
        )
        self.assertEqual('fresh-token', token)
        self.assertTrue(backend.config.verify_created_draft)


class GraphDraftRecoveryTests(unittest.TestCase):
    @staticmethod
    def _response(status_code, payload=None):
        response = MagicMock(status_code=status_code)
        if payload is None:
            response.json.side_effect = ValueError('no JSON body')
        else:
            response.json.return_value = payload
        return response

    @staticmethod
    def _candidate(**changes):
        candidate = {
            'id': 'existing-draft-id',
            'isDraft': True,
            'subject': 'Subject',
            'body': {'contentType': 'html', 'content': '<div>Body</div>'},
            'toRecipients': [{
                'emailAddress': {'address': 'alice@example.com'},
            }],
        }
        candidate.update(changes)
        return candidate

    def _backend(self, candidates, *, identity='master@example.com'):
        transport = MagicMock(side_effect=[
            self._response(200, {'userPrincipalName': identity}),
            self._response(200, {'value': candidates}),
        ])
        draft_transport = MagicMock()
        send_transport = MagicMock()
        backend = GraphReadonlyBackend(
            config=GraphBackendConfig(
                tenant_id='tenant', client_id='client',
                mailbox='master@example.com', verify_created_draft=True,
            ),
            token_store=SimpleNamespace(get_access_token=lambda: 'runtime-token'),
            transport=transport,
            draft_transport=draft_transport,
            send_transport=send_transport,
        )
        return backend, transport, draft_transport, send_transport

    @staticmethod
    def _request():
        return MailDraftRequest(
            to='alice@example.com', subject='Subject', body='Body',
        )

    def test_unique_exact_draft_recovers_stable_reference_read_only(self):
        backend, transport, post, send = self._backend([self._candidate()])

        result = backend.recover_draft(self._request())

        self.assertIs(BackendStatus.READY, result.status)
        self.assertEqual('existing-draft-id', result.draft_reference)
        self.assertEqual('GRAPH_DRAFT_ID', result.reference_kind)
        self.assertEqual(2, transport.call_count)
        self.assertIn('isDraft+eq+true', transport.call_args.args[0])
        post.assert_not_called()
        send.assert_not_called()

    def test_zero_or_multiple_exact_matches_fail_closed(self):
        for candidates, expected in (
            ([], BackendStatus.DRAFT_NOT_FOUND),
            ([self._candidate(), self._candidate(id='second')],
             BackendStatus.INVALID_DRAFT),
        ):
            with self.subTest(count=len(candidates)):
                backend, _, post, send = self._backend(candidates)
                result = backend.recover_draft(self._request())
                self.assertIs(expected, result.status)
                self.assertIsNone(result.draft_reference)
                post.assert_not_called()
                send.assert_not_called()

    def test_metadata_mismatches_and_non_draft_fail_closed(self):
        variants = (
            self._candidate(isDraft=False),
            self._candidate(toRecipients=[{
                'emailAddress': {'address': 'other@example.com'},
            }]),
            self._candidate(subject='Other'),
            self._candidate(body={'contentType': 'text', 'content': 'Other'}),
        )
        for candidate in variants:
            with self.subTest(candidate=candidate):
                backend, _, post, send = self._backend([candidate])
                result = backend.recover_draft(self._request())
                self.assertIs(BackendStatus.DRAFT_NOT_FOUND, result.status)
                self.assertIsNone(result.draft_reference)
                post.assert_not_called()
                send.assert_not_called()

    def test_mailbox_identity_mismatch_stops_before_lookup(self):
        backend, transport, post, send = self._backend(
            [self._candidate()], identity='other@example.com',
        )
        result = backend.recover_draft(self._request())
        self.assertIs(BackendStatus.IDENTITY_MISMATCH, result.status)
        self.assertEqual(1, transport.call_count)
        post.assert_not_called()
        send.assert_not_called()

    def test_acceptance_recovery_returns_create_contract_without_post(self):
        recovered = MailDraftResult(
            BackendStatus.READY, 'Recovered',
            'existing-draft-id', 'GRAPH_DRAFT_ID',
        )
        backend = SimpleNamespace(recover_draft=lambda request: recovered)
        with patch.object(mail_draft, '_graph_backend', return_value=backend):
            result = mail_draft._recover_master_draft_after_known_failure(
                self._request(),
            )
        self.assertEqual('READY', result['status'])
        self.assertEqual('GRAPH_API', result['backend'])
        self.assertEqual('existing-draft-id', result['draft_reference'])
        self.assertEqual('GRAPH_DRAFT_ID', result['reference_kind'])
        self.assertFalse(result['sent'])
        self.assertFalse(result['send_attempted'])

        with patch.object(mail_send, '_send_existing_draft') as send:
            confirmation = mail_send.send_mail_draft(
                'master_mail', result['draft_reference'], False,
            )
        send.assert_not_called()
        self.assertEqual('CONFIRMATION_REQUIRED', confirmation['status'])
        self.assertFalse(confirmation['send_attempted'])


class EdgeDraftTests(unittest.TestCase):
    @staticmethod
    def _control(name, control_type):
        control = MagicMock()
        control.element_info = SimpleNamespace(
            name=name, control_type=control_type,
        )
        return control

    def test_edge_compose_flow_uses_explicit_save_control(self):
        window = MagicMock()
        window.descendants.return_value = []
        new_control = self._control('New mail', 'Button')
        to_control = self._control('To', 'Edit')
        subject_control = self._control('Add a subject', 'Edit')
        body_control = self._control('Message body', 'Document')
        save_control = self._control('Save draft', 'Button')
        controls = [
            new_control, to_control, subject_control, body_control, save_control,
        ]

        def find(window_arg, names, accepted_types, timeout=12.0):
            self.assertIs(window, window_arg)
            requested = {name.casefold() for name in names}
            return next(
                control for control in controls
                if control.element_info.name.casefold() in requested
            )

        with (
            patch.object(mail_draft, '_edge_window', return_value=window),
            patch.object(
                mail_draft,
                '_wait_for_named_control',
                side_effect=find,
            ),
        ):
            reference = mail_draft._run_edge_compose_flow(
                MAILBOX_IDENTITIES['master_mail'],
                123,
                MailDraftRequest(
                    to='alice@example.com',
                    subject='Subject (final)',
                    body='Body + text',
                ),
            )

        self.assertIn('edge:master_mail:draft:', reference)
        new_control.invoke.assert_called_once_with()
        save_control.invoke.assert_called_once_with()
        for control, value in (
            (to_control, 'alice@example.com'),
            (subject_control, 'Subject {(}final{)}'),
            (body_control, 'Body {+} text'),
        ):
            control.set_focus.assert_called_once_with()
            self.assertEqual(
                ['^a{BACKSPACE}', value],
                [call.args[0] for call in control.type_keys.call_args_list],
            )

    def test_edge_identity_mismatch_stops_before_ui(self):
        with (
            patch.object(
                mail_draft,
                '_ensure_mailbox_page',
                return_value=({'controls': []}, 'IDENTITY_MISMATCH'),
            ) as ensure_page,
            patch.object(mail_draft, '_run_edge_compose_flow') as compose,
        ):
            result = mail_draft._create_with_edge(
                MAILBOX_IDENTITIES['qq_mail'],
                MailDraftRequest(
                    to='alice@example.com', subject='Subject', body='Body',
                ),
            )
        ensure_page.assert_called_once()
        compose.assert_not_called()
        self.assertIs(BackendStatus.IDENTITY_MISMATCH, result.status)

    def test_edge_page_not_ready_is_fallback_required(self):
        with patch.object(
            mail_draft,
            '_ensure_mailbox_page',
            return_value=(None, 'PAGE_NOT_READY'),
        ):
            result = mail_draft._create_with_edge(
                MAILBOX_IDENTITIES['bachelor_mail'],
                MailDraftRequest(
                    to='alice@example.com', subject='Subject', body='Body',
                ),
            )
        self.assertIs(BackendStatus.FALLBACK_REQUIRED, result.status)
        self.assertIsNone(result.draft_reference)

    def test_edge_backend_failure_is_reported(self):
        context = SimpleNamespace(hwnd=123, profile_directory='Profile 3')
        with (
            patch.object(
                mail_draft,
                '_ensure_mailbox_page',
                return_value=({'controls': []}, 'READY'),
            ),
            patch.object(
                mail_draft,
                'get_runtime_mailbox_context',
                return_value=context,
            ),
            patch.object(mail_draft, '_focus_window_handle'),
            patch.object(
                mail_draft,
                '_run_edge_compose_flow',
                side_effect=RuntimeError('compose unavailable'),
            ),
        ):
            result = mail_draft._create_with_edge(
                MAILBOX_IDENTITIES['qq_mail'],
                MailDraftRequest(
                    to='alice@example.com', subject='Subject', body='Body',
                ),
            )
        self.assertIs(BackendStatus.REQUEST_FAILED, result.status)
        self.assertIsNone(result.draft_reference)


class DraftDispatchTests(unittest.TestCase):
    @staticmethod
    def _ready_draft(reference='draft-reference'):
        return MailDraftResult(
            BackendStatus.READY,
            'Draft saved',
            reference,
            'EDGE_DRAFT_HASH',
        )

    def test_outlook_graph_failures_fall_back_to_verified_edge(self):
        for status in (
            BackendStatus.NOT_AUTHENTICATED,
            BackendStatus.TOKEN_EXPIRED,
        ):
            graph = SimpleNamespace(create_draft=lambda request: MailDraftResult(
                status, 'Graph unavailable',
            ))
            with (
                patch.object(
                    mail_draft, '_backend_for_identity', return_value=graph,
                ),
                patch.object(
                    mail_draft,
                    '_create_with_edge',
                    return_value=self._ready_draft(),
                ) as edge,
            ):
                result = mail_draft._create_draft_mailbox(
                    MAILBOX_IDENTITIES['master_mail'],
                    MailDraftRequest(
                        to='alice@example.com', subject='Subject', body='Body',
                    ),
                )
            edge.assert_called_once()
            self.assertEqual('EDGE_GUI', result['backend'])
            self.assertEqual('READY', result['status'])
            self.assertIn('Graph draft unavailable', result['message'])
            self.assertFalse(result['sent'])

    def test_graph_request_failure_fails_closed_without_edge_side_effect(self):
        graph = SimpleNamespace(create_draft=lambda request: MailDraftResult(
            BackendStatus.REQUEST_FAILED, 'Graph request failed',
        ))
        with (
            patch.object(mail_draft, '_backend_for_identity', return_value=graph),
            patch.object(mail_draft, '_create_with_edge') as edge,
        ):
            result = mail_draft._create_draft_mailbox(
                MAILBOX_IDENTITIES['master_mail'],
                MailDraftRequest(to='alice@example.com', subject='Subject', body='Body'),
            )
        edge.assert_not_called()
        self.assertEqual('ERROR', result['status'])
        self.assertFalse(result['sent'])

    def test_invalid_graph_draft_maps_to_error_without_exception(self):
        graph = SimpleNamespace(create_draft=lambda request: MailDraftResult(
            BackendStatus.INVALID_DRAFT, 'Draft metadata mismatch',
        ))
        with patch.object(
            mail_draft, '_backend_for_identity', return_value=graph,
        ):
            result = mail_draft._create_draft_mailbox(
                MAILBOX_IDENTITIES['master_mail'],
                MailDraftRequest(to='alice@example.com', subject='Subject', body='Body'),
            )
        self.assertEqual('ERROR', result['status'])
        self.assertIsNone(result['draft_reference'])

    def test_outlook_graph_identity_mismatch_does_not_fall_back(self):
        graph = SimpleNamespace(create_draft=lambda request: MailDraftResult(
            BackendStatus.IDENTITY_MISMATCH, 'Wrong Graph account',
        ))
        with (
            patch.object(mail_draft, '_backend_for_identity', return_value=graph),
            patch.object(mail_draft, '_create_with_edge') as edge,
        ):
            result = mail_draft._create_draft_mailbox(
                MAILBOX_IDENTITIES['master_mail'],
                MailDraftRequest(
                    to='alice@example.com', subject='Subject', body='Body',
                ),
            )
        edge.assert_not_called()
        self.assertEqual('IDENTITY_MISMATCH', result['status'])

    def test_qq_and_bachelor_use_edge_backend(self):
        for mailbox_id in ('bachelor_mail', 'qq_mail'):
            with patch.object(
                mail_draft,
                '_create_with_edge',
                return_value=self._ready_draft(),
            ) as edge:
                result = mail_draft._create_draft_mailbox(
                    MAILBOX_IDENTITIES[mailbox_id],
                    MailDraftRequest(
                        to='alice@example.com', subject='Subject', body='Body',
                    ),
                )
            edge.assert_called_once()
            self.assertEqual('EDGE_GUI', result['backend'])
            self.assertEqual('READY', result['status'])

    def test_missing_draft_permission_is_rejected(self):
        identity = SimpleNamespace(
            mailbox_id='forbidden',
            display_name='Forbidden',
            permissions=('READ',),
        )
        with patch.object(mail_draft, '_backend_for_identity') as backend:
            result = mail_draft._create_draft_mailbox(
                identity,
                MailDraftRequest(
                    to='alice@example.com', subject='Subject', body='Body',
                ),
            )
        backend.assert_not_called()
        self.assertEqual('ERROR', result['status'])
        self.assertFalse(result['sent'])

    def test_unknown_mailbox_and_invalid_recipient_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown mailbox identity'):
            mail_draft.create_mail_draft(
                'missing', 'alice@example.com', 'Subject', 'Body',
            )
        with self.assertRaisesRegex(ValueError, 'valid email address'):
            mail_draft.create_mail_draft(
                'qq_mail', 'not-an-email', 'Subject', 'Body',
            )

    def test_external_return_shape_and_normalization(self):
        captured = {}

        def capture(identity, request):
            captured['identity'] = identity
            captured['request'] = request
            return {
                'mailbox_id': identity.mailbox_id,
                'display_name': identity.display_name,
                'backend': 'EDGE_GUI',
                'status': 'READY',
                'message': 'Draft saved',
                'to': 'alice@example.com',
                'subject': 'Subject',
                'draft_reference': 'edge:qq_mail:draft:value',
                'reference_kind': 'EDGE_DRAFT_HASH',
                'sent': False,
                'send_attempted': False,
            }

        with patch.object(
            mail_draft, '_create_draft_mailbox', side_effect=capture,
        ):
            result = mail_draft.create_mail_draft(
                'qq_mail',
                'Alice <alice@example.com>',
                '  Subject  ',
                'Body\r\n',
            )
        self.assertEqual(MAILBOX_IDENTITIES['qq_mail'], captured['identity'])
        self.assertEqual('alice@example.com', captured['request'].to)
        self.assertEqual('Subject', captured['request'].subject)
        self.assertEqual('Body', captured['request'].body)
        self.assertEqual({
            'mailbox_id', 'display_name', 'backend', 'status', 'message',
            'draft_reference', 'reference_kind', 'to', 'subject', 'sent',
            'send_attempted',
        }, set(result))
        self.assertFalse(result['sent'])
        self.assertFalse(result['send_attempted'])


if __name__ == '__main__':
    unittest.main()
