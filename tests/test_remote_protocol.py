"""Unit tests for remote command protocol primitives."""

import unittest

from windows_gui.remote import protocol


def envelope(command='health.read', request_id='a' * 16, params=None, **extra):
    payload = {'command': command, 'request_id': request_id}
    if params is not None:
        payload['params'] = params
    payload.update(extra)
    return payload


class CommandEnumTests(unittest.TestCase):
    def test_command_allowlist_is_exact(self):
        self.assertEqual(
            {
                'health.read', 'task.status', 'task.cancel',
                'browser.request_click', 'browser.request_download',
                'mail.request_draft', 'session.revoke_self',
            },
            set(protocol.COMMANDS),
        )

    def test_stage_commands_require_local_confirmation(self):
        for name in (
            'browser.request_click', 'browser.request_download',
            'mail.request_draft',
        ):
            spec = protocol.COMMANDS[name]
            self.assertEqual(2, spec.level)
            self.assertTrue(spec.mutating)
            self.assertTrue(spec.stages_task)
            self.assertTrue(spec.requires_local_confirmation)

    def test_read_commands_never_stage_or_mutate(self):
        for name in ('health.read', 'task.status'):
            spec = protocol.COMMANDS[name]
            self.assertEqual(0, spec.level)
            self.assertFalse(spec.mutating)
            self.assertFalse(spec.stages_task)


class EnvelopeParsingTests(unittest.TestCase):
    def test_minimal_parameterless_command_parses(self):
        spec, request_id, params = protocol.parse_request_envelope(envelope())
        self.assertEqual('health.read', spec.name)
        self.assertEqual('a' * 16, request_id)
        self.assertEqual({}, params)

    def test_bytes_envelope_parses_with_size_limit(self):
        import json

        raw = json.dumps(envelope()).encode('utf-8')
        spec, _, _ = protocol.parse_request_envelope(raw)
        self.assertEqual('health.read', spec.name)
        with self.assertRaises(protocol.InvalidRequestError):
            protocol.parse_request_envelope(b'x' * (protocol.MAX_BODY_BYTES + 1))

    def test_unknown_command_is_rejected(self):
        with self.assertRaises(protocol.UnknownCommandError):
            protocol.parse_request_envelope(envelope(command='shell.exec'))

    def test_malformed_request_id_is_rejected(self):
        for bad in ('short', 'x' * 15, 'x' * 129, 'bad id with spaces'):
            with self.subTest(request_id=bad):
                with self.assertRaises(protocol.InvalidRequestError):
                    protocol.parse_request_envelope(envelope(request_id=bad))

    def test_unsupported_envelope_fields_are_rejected(self):
        with self.assertRaises(protocol.InvalidRequestError):
            protocol.parse_request_envelope(envelope(admin_bypass=True))

    def test_non_dict_envelope_is_rejected(self):
        with self.assertRaises(protocol.InvalidRequestError):
            protocol.parse_request_envelope([envelope()])


class ParameterValidationTests(unittest.TestCase):
    def test_click_params_validate(self):
        _, _, params = protocol.parse_request_envelope(envelope(
            command='browser.request_click',
            params={'text': 'Submit', 'exact': False},
        ))
        self.assertEqual({'text': 'Submit', 'exact': False}, params)

    def test_click_text_bounds_and_control_characters(self):
        for params in (
            {'text': ''},
            {'text': 'x' * 501},
            {'text': 'two\nlines'},
        ):
            with self.subTest(params=params):
                with self.assertRaises(protocol.InvalidRequestError):
                    protocol.parse_request_envelope(envelope(
                        command='browser.request_click', params=params,
                    ))

    def test_click_exact_must_be_boolean(self):
        with self.assertRaises(protocol.InvalidRequestError):
            protocol.parse_request_envelope(envelope(
                command='browser.request_click',
                params={'text': 'Submit', 'exact': 'yes'},
            ))

    def test_download_filename_bounds(self):
        with self.assertRaises(protocol.InvalidRequestError):
            protocol.parse_request_envelope(envelope(
                command='browser.request_download',
                params={'text': 'Download', 'filename': 'x' * 241},
            ))

    def test_draft_params_validate_for_all_mailboxes(self):
        for mailbox in ('master_mail', 'bachelor_mail', 'qq_mail'):
            with self.subTest(mailbox=mailbox):
                _, _, params = protocol.parse_request_envelope(envelope(
                    command='mail.request_draft',
                    params={
                        'mailbox_id': mailbox, 'to': 'teacher@cuc.edu.cn',
                        'subject': 'Hello', 'body': 'Content',
                    },
                ))
                self.assertEqual(mailbox, params['mailbox_id'])

    def test_draft_param_rejections(self):
        base = {
            'mailbox_id': 'master_mail', 'to': 'teacher@cuc.edu.cn',
            'subject': 'Hello', 'body': 'Content',
        }
        cases = [
            {**base, 'mailbox_id': 'gmail_mail'},
            {**base, 'to': 'not-an-email'},
            {**base, 'to': 'a@b c.d'},
            {**base, 'subject': ''},
            {**base, 'subject': 'x' * 201},
            {**base, 'body': 'x' * 50_001},
        ]
        for params in cases:
            with self.subTest(params=params):
                with self.assertRaises(protocol.InvalidRequestError):
                    protocol.parse_request_envelope(envelope(
                        command='mail.request_draft', params=params,
                    ))

    def test_task_cancel_params(self):
        _, _, params = protocol.parse_request_envelope(envelope(
            command='task.cancel', params={'task_id': 'a' * 8},
        ))
        self.assertEqual('a' * 8, params['task_id'])
        with self.assertRaises(protocol.InvalidRequestError):
            protocol.parse_request_envelope(envelope(
                command='task.cancel', params={'task_id': 'short'},
            ))


class IdempotencyCacheTests(unittest.TestCase):
    def test_round_trip_and_device_isolation(self):
        cache = protocol.IdempotencyCache()
        cache.put('device-1', 'request-1', {'ok': True})
        self.assertEqual({'ok': True}, cache.get('device-1', 'request-1'))
        self.assertIsNone(cache.get('device-2', 'request-1'))

    def test_ttl_expiry(self):
        clock = {'now': 100.0}
        cache = protocol.IdempotencyCache(ttl_seconds=10, now_factory=lambda: clock['now'])
        cache.put('device-1', 'request-1', {'ok': True})
        clock['now'] += 11
        self.assertIsNone(cache.get('device-1', 'request-1'))

    def test_capacity_evicts_oldest(self):
        cache = protocol.IdempotencyCache(capacity=2)
        cache.put('d', 'r1', {'n': 1})
        cache.put('d', 'r2', {'n': 2})
        cache.put('d', 'r3', {'n': 3})
        self.assertIsNone(cache.get('d', 'r1'))
        self.assertEqual({'n': 2}, cache.get('d', 'r2'))


if __name__ == '__main__':
    unittest.main()
