"""Deterministic C.1 regressions; no network or credential access."""

import unittest
from unittest.mock import Mock, patch

from windows_gui import imap_mail as im, mail_summary as ms
from windows_gui.mail_backends import BackendStatus, GraphBackendConfig


class ReadAcceptanceTests(unittest.TestCase):
    def test_qq_single_digit_internaldate(self):
        self.assertEqual(im._parse_internal_date(b'1 (INTERNALDATE "05-Sep-2026 09:00:00 +0800")'),
                         im._parse_internal_date(b'1 (INTERNALDATE "5-Sep-2026 09:00:00 +0800")'))

    def test_summary_uses_refresh_and_checks_identity(self):
        transport=Mock(side_effect=[Mock(status_code=200,json=lambda:{'mail':'fixture@example.com'}),
                                    Mock(status_code=200,json=lambda:{'value':[]})])
        backend=ms.SummaryGraphBackend(config=GraphBackendConfig(tenant_id='t',client_id='c',mailbox='fixture@example.com'),
                                      token_store=Mock(),transport=transport)
        with patch('windows_gui.mail_digest.refresh_master_graph_token',return_value={'access_token':'fixture-token'}) as refresh:
            result=backend.summarize_today(10)
        self.assertEqual(BackendStatus.READY,result.status)
        refresh.assert_called_once()
        backend.token_store.get_access_token.assert_not_called()
        self.assertEqual(2,transport.call_count)

    def test_summary_identity_mismatch_never_reads_list(self):
        transport=Mock(return_value=Mock(status_code=200,json=lambda:{'mail':'other@example.com'}))
        backend=ms.SummaryGraphBackend(config=GraphBackendConfig(tenant_id='t',client_id='c',mailbox='fixture@example.com'),token_store=Mock(),transport=transport)
        with patch('windows_gui.mail_digest.refresh_master_graph_token',return_value={'access_token':'fixture-token'}):
            self.assertEqual(BackendStatus.IDENTITY_MISMATCH,backend.summarize_today(10).status)
        self.assertEqual(1,transport.call_count)

    def test_missing_date_is_not_invented(self):
        self.assertIsNone(im._parse_internal_date(b'1 (UID 10)'))

    def test_malformed_graph_list_is_not_empty_success(self):
        transport=Mock(side_effect=[Mock(status_code=200,json=lambda:{'mail':'fixture@example.com'}),
                                    Mock(status_code=200,json=lambda:{})])
        backend=ms.SummaryGraphBackend(config=GraphBackendConfig(tenant_id='t',client_id='c',mailbox='fixture@example.com'),token_store=Mock(),transport=transport)
        with patch('windows_gui.mail_digest.refresh_master_graph_token',return_value={'access_token':'fixture-token'}):
            self.assertEqual(BackendStatus.REQUEST_FAILED,backend.summarize_today(10).status)

    def test_refresh_failure_does_not_read_list_or_expose_secret(self):
        transport=Mock()
        backend=ms.SummaryGraphBackend(config=GraphBackendConfig(tenant_id='t',client_id='c',mailbox='fixture@example.com'),token_store=Mock(),transport=transport)
        with patch('windows_gui.mail_digest.refresh_master_graph_token',side_effect=RuntimeError('SECRET')):
            result=backend.summarize_today(10)
        self.assertEqual(BackendStatus.REQUEST_FAILED,result.status)
        self.assertNotIn('SECRET',result.message)
        transport.assert_not_called()

    def test_graph_identity_http_failure_never_reads_list(self):
        transport = Mock(return_value=Mock(status_code=403))
        backend = ms.SummaryGraphBackend(
            config=GraphBackendConfig(
                tenant_id='t', client_id='c', mailbox='fixture@example.com'
            ),
            token_store=Mock(),
            transport=transport,
        )
        with patch(
            'windows_gui.mail_digest.refresh_master_graph_token',
            return_value={'access_token': 'fixture-token'},
        ):
            result = backend.summarize_today(10)
        self.assertEqual(BackendStatus.REQUEST_FAILED, result.status)
        self.assertEqual(1, transport.call_count)
