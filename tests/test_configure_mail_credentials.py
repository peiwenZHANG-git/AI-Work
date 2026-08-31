"""Security tests for interactive mail credential configuration."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from contextlib import redirect_stdout
import unittest
from unittest import mock

from windows_gui.mail_backends import WindowsCredentialManagerSecretStore


def _load_cli_module():
    path = (
        Path(__file__).resolve().parents[1]
        / 'scripts'
        / 'configure_mail_credentials.py'
    )
    spec = importlib.util.spec_from_file_location(
        'test_configure_mail_credentials_target', path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigureMailCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.cli = _load_cli_module()

    def test_whitelist_excludes_master_oauth_and_matches_assistant_names(self):
        self.assertNotIn('master_graph_refresh', self.cli.CREDENTIAL_TARGETS)
        self.assertEqual(
            (
                'qq_assistant_draft',
                'bachelor_assistant_draft',
                'bachelor_assistant_smtp',
            ),
            self.cli.ASSISTANT_KEYS,
        )

    def test_store_secret_uses_dedicated_target_and_rejects_invalid_secret(self):
        created = []

        def factory(service, username):
            store = mock.Mock(spec=WindowsCredentialManagerSecretStore)
            created.append((service, username, store))
            return store

        self.cli.store_secret(
            'qq_assistant_draft', 'runtime-secret', store_factory=factory
        )

        self.assertEqual(
            [
                (
                    'AI-Work/windows-gui/mailboxes',
                    'qq_mail_assistant_draft_authorization_code',
                )
            ],
            [(service, username) for service, username, _store in created],
        )
        created[0][2].set_secret.assert_called_once_with('runtime-secret')

        invalid_store = mock.Mock(spec=WindowsCredentialManagerSecretStore)
        invalid_store.set_secret.side_effect = ValueError('empty credential')

        def invalid_factory(service, username):
            return invalid_store

        with self.assertRaises(self.cli.CredentialConfigurationError):
            self.cli.store_secret(
                'qq_assistant_draft', '', store_factory=invalid_factory
            )
        invalid_store.set_secret.assert_called_once_with('')

    def test_prompt_requires_matching_secret_and_does_not_disclose_it(self):
        outputs = []
        prompts = iter(['runtime-secret', 'runtime-secret'])
        store = mock.Mock(spec=WindowsCredentialManagerSecretStore)
        store.get_secret.side_effect = [None, True]

        def factory(service, username):
            store.service = service
            store.username = username
            return store

        changed = self.cli.prompt_and_store(
            'bachelor_assistant_smtp',
            prompt=lambda _label: next(prompts),
            output=outputs.append,
            store_factory=factory,
        )

        self.assertTrue(changed)
        store.set_secret.assert_called_once_with('runtime-secret')
        self.assertIn('[OK] bachelor_assistant_smtp: saved', outputs)
        self.assertNotIn('runtime-secret', '\n'.join(outputs))

    def test_existing_target_is_skipped_without_force(self):
        prompts = []
        outputs = []
        store = mock.Mock(spec=WindowsCredentialManagerSecretStore)
        store.get_secret.return_value = 'existing-secret'

        changed = self.cli.prompt_and_store(
            'qq_assistant_draft',
            prompt=lambda label: prompts.append(label),
            output=outputs.append,
            store_factory=lambda service, username: store,
        )

        self.assertFalse(changed)
        self.assertEqual([], prompts)
        self.assertIn('[SKIP] qq_assistant_draft: already configured', outputs)
        store.set_secret.assert_not_called()

    def test_mismatched_prompt_does_not_write(self):
        prompts = iter(['first', 'second'])
        store = mock.Mock(spec=WindowsCredentialManagerSecretStore)
        store.get_secret.return_value = None

        with self.assertRaises(self.cli.CredentialConfigurationError):
            self.cli.prompt_and_store(
                'glm_api_key',
                prompt=lambda _label: next(prompts),
                output=lambda _text: None,
                store_factory=lambda service, username: store,
            )

        store.set_secret.assert_not_called()

    def test_missing_assistant_command_updates_only_three_targets(self):
        stores = []

        class FakeStore:
            values: dict[str, str] = {}

            def __init__(self, service, username):
                self.service = service
                self.username = username
                stores.append((service, username))

            def get_secret(self):
                return self.values.get(self.username)

            def set_secret(self, secret):
                self.values[self.username] = secret
        FakeStore.values = {}
        prompts = iter(
            [
                'code-1', 'code-1',
                'code-3', 'code-3',
                'code-5', 'code-5',
            ]
        )
        outputs = []
        with mock.patch.object(
            self.cli, 'WindowsCredentialManagerSecretStore', FakeStore
        ):
            with redirect_stdout(io.StringIO()):
                exit_code = self.cli.main(
                    ['--missing-assistant'], prompt=lambda _label: next(prompts)
                )

        self.assertEqual(0, exit_code)
        self.assertEqual(
            {
                'qq_mail_assistant_draft_authorization_code': 'code-1',
                'bachelor_mail_assistant_draft_authorization_code': 'code-3',
                'bachelor_mail_assistant_smtp_authorization_code': 'code-5',
            },
            FakeStore.values,
        )
        FakeStore.values = {}


if __name__ == '__main__':
    unittest.main()
