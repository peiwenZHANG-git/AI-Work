"""Verify that enrich_digests reuses the persisted translation cache."""

import unittest
from types import SimpleNamespace

from windows_gui.mail_digest import (
    CLASSIFICATION_POLICY_VERSION,
    DigestMail,
    MailboxDigest,
    enrich_digests,
)


class CacheLoadTest(unittest.TestCase):
    def test_enrich_uses_persisted_cache_when_not_passed_explicitly(self) -> None:
        box = MailboxDigest(
            mailbox_id='qq_mail',
            display_name='QQ 邮箱',
            short_name='QQ',
            status='READY',
            message='',
            emails=[DigestMail('a', 's1', '', body_text='正文内容')],
        )
        cache_key = __import__('hashlib').sha256(
            'qq_mail|s1|正文内容'.encode('utf-8')
        ).hexdigest()[:40]
        seeded = {cache_key: {
            'summary': '缓存摘要',
            'translation': '缓存翻译',
            'classification_policy_version': CLASSIFICATION_POLICY_VERSION,
        }}
        original_load = None
        import windows_gui.mail_digest as module
        original_load = module.load_translation_cache
        module.load_translation_cache = lambda: seeded
        original_save = module.save_translation_cache
        module.save_translation_cache = lambda cache: None
        try:
            def failing_post(url, headers=None, json=None, timeout=None):
                raise AssertionError('API should not be called on cache hit')

            enrich_digests([box], 'test-key', transport=failing_post)
        finally:
            module.load_translation_cache = original_load
            module.save_translation_cache = original_save
        self.assertEqual(box.emails[0].summary, '缓存摘要')
        self.assertEqual(box.emails[0].translation, '缓存翻译')


if __name__ == '__main__':
    unittest.main()
