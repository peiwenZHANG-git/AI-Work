"""Verify assistant credentials stay separate from read-only mailbox credentials."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import windows_gui.mail_assistant as mail_assistant


class AssistantCredentialSeparationTests(unittest.TestCase):
    def test_draft_and_smtp_accounts_use_dedicated_entries(self):
        captured = []

        def fake_store(service, username):
            captured.append((service, username))
            return SimpleNamespace(get_secret=lambda: 'runtime-secret')

        config = SimpleNamespace(username='student@example.com')
        with mock.patch.dict(
            'os.environ',
            {
                'AI_WORK_QQ_IMAP_USERNAME': 'qq@example.com',
                'AI_WORK_BACHELOR_IMAP_USERNAME': 'student@example.com',
            },
        ), mock.patch(
            'windows_gui.imap_mail.QqImapConfig.from_environment',
            return_value=config,
        ), mock.patch(
            'windows_gui.imap_mail.BachelorImapConfig.from_environment',
            return_value=config,
        ), mock.patch(
            'windows_gui.mail_backends.WindowsCredentialManagerSecretStore',
            fake_store,
        ):
            qq = mail_assistant._assistant_account(
                'qq_mail',
                mail_assistant.ASSISTANT_DRAFT_CREDENTIAL_USERNAMES['qq_mail'],
            )
            bachelor = mail_assistant._assistant_account(
                'bachelor_mail',
                mail_assistant.BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME,
            )

        self.assertEqual(
            ('AI-Work/windows-gui/mailboxes', 'qq_mail_assistant_draft_authorization_code'),
            captured[0],
        )
        self.assertEqual(
            (
                'AI-Work/windows-gui/mailboxes',
                'bachelor_mail_assistant_smtp_authorization_code',
            ),
            captured[1],
        )
        self.assertEqual('runtime-secret', qq['password'])
        self.assertEqual('runtime-secret', bachelor['password'])


if __name__ == '__main__':
    unittest.main()
