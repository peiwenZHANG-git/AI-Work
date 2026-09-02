"""Unit tests for the nightly digest formatting and toast helpers."""

import hashlib
import json
import os
import requests
import threading
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace
import tempfile
import unittest.mock as mock

from windows_gui.mail_digest import (
    AttachmentInfo,
    MailboxDigest,
    DigestMail,
    _mail_cache_key,
    _prune_notified,
    select_new_high,
    build_toast_lines,
    build_toast_powershell,
    build_full_ai_prompt,
    format_digest,
    write_run_artifacts,
    format_digest_html,
    enrich_digests,
    call_mail_translation,
    SummaryAPIError,
    clean_body_text,
    extract_body_text,
    extract_attachment_metadata,
    graph_body_text,
    strip_html,
)
import windows_gui.mail_digest as mail_digest


def _mail(sender: str, subject: str, time: str, **extra) -> SimpleNamespace:
    return SimpleNamespace(sender=sender, subject=subject, time=time, **extra)


class MailDigestTest(unittest.TestCase):
    def test_every_mail_has_its_own_confirmed_dismiss_button(self) -> None:
        box = MailboxDigest(
            'qq_mail', 'QQ 邮箱', 'QQ', 'READY', '',
            [
                DigestMail('a', 'one', '2026-09-02T10:00:00+02:00',
                           source_reference='imap:1'),
                DigestMail('b', 'two', '2026-09-02T11:00:00+02:00',
                           source_reference='imap:2'),
            ],
        )

        html = format_digest_html(
            [box], datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        )

        self.assertEqual(2, html.count('class="mail-dismiss"'))
        self.assertEqual(2, html.count('data-key="'))
        self.assertIn('await fetch("/api/dismiss"', html)
        self.assertIn('b.textContent="保存中"', html)
        self.assertIn('if(!response.ok)throw', html)

    def test_mailbox_collection_is_concurrent_and_preserves_order(self) -> None:
        barrier = threading.Barrier(3)

        def collector(mailbox_id):
            def collect():
                barrier.wait(timeout=1)
                return MailboxDigest(
                    mailbox_id, mailbox_id, mailbox_id, 'READY', ''
                )
            return collect

        result = mail_digest._collect_mailbox_digests(tuple(
            collector(mailbox_id)
            for mailbox_id in ('qq_mail', 'bachelor_mail', 'master_mail')
        ))

        self.assertEqual(
            ['qq_mail', 'bachelor_mail', 'master_mail'],
            [box.mailbox_id for box in result],
        )

    def test_format_digest_lists_ready_empty_and_failed_mailboxes(self) -> None:
        mailboxes = [
            MailboxDigest(
                mailbox_id='qq_mail',
                display_name='QQ 邮箱',
                short_name='QQ',
                status='READY',
                message='',
                emails=[
                    _mail('A <a@example.com>', '会议通知', '2026-08-29T21:02:00'),
                    _mail('B <b@example.com>', '报告', '2026-08-29T09:00:00+08:00'),
                ],
            ),
            MailboxDigest(
                mailbox_id='bachelor_mail',
                display_name='传媒大学本科邮箱',
                short_name='本科',
                status='READY',
                message='',
                emails=[],
            ),
            MailboxDigest(
                mailbox_id='master_mail',
                display_name='巴黎萨克雷邮箱',
                short_name='萨克雷',
                status='TOKEN_EXPIRED',
                message='Graph 令牌刷新失败：invalid_grant',
            ),
        ]
        generated = datetime(2026, 8, 29, 21, 30, 5)
        text = format_digest(mailboxes, generated)
        self.assertIn('每日邮件摘要 · 2026-08-29', text)
        self.assertIn('【QQ 邮箱】', text)
        self.assertIn('  2 封', text)
        self.assertIn('21:02 A <a@example.com> — 会议通知', text)
        self.assertIn('[低]', text)
        self.assertIn('过去 24 小时没有新邮件', text)
        self.assertIn('读取失败（TOKEN_EXPIRED）', text)
        self.assertIn('只读模式：正文仅用于生成摘要；卡片可标记"已读"仅隐藏显示', text)

    def test_format_digest_truncates_long_fields(self) -> None:
        box = MailboxDigest(
            mailbox_id='qq_mail',
            display_name='QQ 邮箱',
            short_name='QQ',
            status='READY',
            message='',
            emails=[
                _mail('x' * 120, 'y' * 120, '2026-08-29T08:30:00+00:00'),
            ],
        )
        text = format_digest([box], datetime(2026, 8, 29, 10, 0, 0))
        subject_line = next(
            line for line in text.splitlines() if line.strip().startswith('1.')
        )
        self.assertLessEqual(len(subject_line), 130)
        self.assertIn('…', subject_line)

    def test_build_toast_lines_counts_and_truncates(self) -> None:
        mailboxes = [
            MailboxDigest(
                mailbox_id='qq_mail',
                display_name='QQ 邮箱',
                short_name='QQ',
                status='READY',
                message='',
                emails=[_mail('a', '第一封邮件标题非常长' * 10, '')],
            ),
            MailboxDigest(
                mailbox_id='bachelor_mail',
                display_name='传媒大学本科邮箱',
                short_name='本科',
                status='READY',
                message='',
            ),
            MailboxDigest(
                mailbox_id='master_mail',
                display_name='巴黎萨克雷邮箱',
                short_name='萨克雷',
                status='REQUEST_FAILED',
                message='',
            ),
        ]
        title, counts, subjects = build_toast_lines(
            mailboxes, datetime(2026, 8, 29, 21, 30)
        )
        self.assertEqual(title, '每日邮件摘要 08-29')
        self.assertEqual(counts, 'QQ 1封 · 本科 0封 · 萨克雷 ✕封')
        self.assertLessEqual(len(subjects), 30)
        self.assertTrue(subjects.startswith('第一封邮件标题'))

    def test_empty_today_counts_as_healthy_no_mail(self) -> None:
        box = MailboxDigest(
            mailbox_id='qq_mail',
            display_name='QQ 邮箱',
            short_name='QQ',
            status='EMPTY_TODAY',
            message='QQ IMAP 已确认收件箱今天没有邮件',
        )
        self.assertTrue(box.ok)
        text = format_digest([box], datetime(2026, 8, 29, 21, 30))
        self.assertIn('过去 24 小时没有新邮件', text)
        self.assertNotIn('读取失败', text)
        _, counts, _ = build_toast_lines([box], datetime(2026, 8, 29, 21, 30))
        self.assertEqual(counts, 'QQ 0封')

    def test_format_digest_html_escapes_and_renders(self) -> None:
        mailboxes = [
            MailboxDigest(
                mailbox_id='qq_mail',
                display_name='QQ 邮箱',
                short_name='QQ',
                status='READY',
                message='',
                emails=[_mail(
                    'A <a@example.com>',
                    '会议 & 通知',
                    '2026-08-29T09:30:00',
                    body_text='Original text here',
                    summary='中文摘要内容',
                    translation='中文翻译内容',
                    attachments=[AttachmentInfo(
                        '作业模板 & 说明.docx',
                        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                        2048,
                    )],
                )],
            ),
            MailboxDigest(
                mailbox_id='bachelor_mail',
                display_name='传媒大学本科邮箱',
                short_name='本科',
                status='EMPTY_TODAY',
                message='',
            ),
            MailboxDigest(
                mailbox_id='master_mail',
                display_name='巴黎萨克雷邮箱',
                short_name='萨克雷',
                status='REQUEST_FAILED',
                message='Graph 请求失败：HTTP 500',
            ),
        ]
        html = format_digest_html(mailboxes, datetime(2026, 8, 29, 21, 30))
        self.assertTrue(html.startswith('<!DOCTYPE html>'))
        self.assertIn('charset="utf-8"', html)
        self.assertIn('过去 24 小时共 1 封', html)
        self.assertIn('imp-low', html)
        self.assertIn('A &lt;a@example.com&gt;', html)
        self.assertIn('会议 &amp; 通知', html)
        self.assertIn('过去 24 小时没有新邮件', html)
        self.assertIn('读取失败（REQUEST_FAILED）', html)
        self.assertIn('部分邮箱读取失败', html)
        self.assertIn('过去 24 小时没有新邮件', html)
        self.assertIn('class="mail-summary"', html)
        self.assertIn('全文翻译', html)
        self.assertIn('class="translation"', html)
        self.assertIn('中文翻译内容', html)
        self.assertIn('查看原文', html)
        self.assertIn('data-toggle-all', html)
        self.assertIn('id="filter-search"', html)
        self.assertIn('id="filter-mailbox"', html)
        self.assertIn('id="filter-importance"', html)
        self.assertIn('id="filter-date"', html)
        self.assertIn('data-mailbox="qq_mail"', html)
        self.assertIn('data-importance="低"', html)
        self.assertIn('data-date="2026-08-29"', html)
        self.assertIn('作业模板 &amp; 说明.docx', html)
        self.assertIn('2.0 KB', html)
        self.assertNotIn('contentBytes', html)
        self.assertEqual(1, html.count('class="mail-dismiss"'))
        self.assertEqual(1, html.count('data-key="'))
        self.assertNotIn('<a@example.com>', html)

    def test_format_digest_html_sorts_by_importance(self) -> None:
        box = MailboxDigest(
            mailbox_id='master_mail',
            display_name='巴黎萨克雷邮箱',
            short_name='萨克雷',
            status='READY',
            message='',
            emails=[
                _mail('a', '普通通知', '2026-08-29T10:00:00', importance='中'),
                _mail('b', '截止日期提醒', '2026-08-29T09:00:00', importance='高'),
            ],
        )
        html = format_digest_html([box], datetime(2026, 8, 29, 21, 30))
        self.assertIn('imp-high', html)
        self.assertIn('截止日期提醒', html)
        self.assertLess(html.index('截止日期提醒'), html.index('普通通知'))

    def test_build_toast_powershell_escapes_xml_and_quotes(self) -> None:
        script = build_toast_powershell("A&B <标题>'", 'C&D', 'E')
        self.assertIn('A&amp;B &lt;标题&gt;\'\'', script)
        self.assertIn('C&amp;D', script)
        self.assertIn('ToastText04', script)


class RunArtifactTest(unittest.TestCase):
    def test_write_run_artifacts_records_complete_status(self):
        mailboxes = [
            SimpleNamespace(
                mailbox_id='qq_mail', status='EMPTY_TODAY', ok=True,
                message='ok', emails=[],
            ),
            SimpleNamespace(
                mailbox_id='master_mail', status='READY', ok=True,
                message='ok', emails=['mail'],
            ),
        ]
        generated = datetime(2026, 8, 31, 10, 0)
        with tempfile.TemporaryDirectory() as directory:
            digest_path = Path(directory) / 'digest.html'
            status_path = Path(directory) / 'last-run.json'
            write_run_artifacts(
                digest_path,
                '<html>ok</html>',
                mailboxes,
                generated,
                True,
                status_path=status_path,
            )
            status = json.loads(status_path.read_text(encoding='utf-8'))
            digest_text = digest_path.read_text(encoding='utf-8')

        self.assertTrue(digest_text.startswith('<html>'))
        self.assertTrue(status['ok'])
        self.assertTrue(status['mailboxes_ok'])
        self.assertTrue(status['toast_shown'])
        self.assertEqual(2, status['mailbox_count'])
        self.assertEqual(1, status['total_mails'])


class RunDigestLockTest(unittest.TestCase):
    def test_lock_busy_is_reported_as_failed_skip(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            mail_digest, '_acquire_run_lock', return_value=False
        ), mock.patch.object(
            mail_digest, '_release_run_lock'
        ) as release:
            attempt_path = Path(directory) / 'last-attempt.json'
            result = mail_digest.run_digest_update(attempt_path=attempt_path)
            payload = json.loads(attempt_path.read_text(encoding='utf-8'))
        self.assertFalse(result['ok'])
        self.assertTrue(result['skipped'])
        self.assertEqual('lock_busy', result['reason'])
        self.assertIsInstance(result['generated_at'], str)
        self.assertTrue(result['generated_at'])
        self.assertEqual('', result['digest_path'])
        release.assert_not_called()
        self.assertEqual('lock_busy', payload['stage'])
        self.assertFalse(payload['ok'])
        self.assertEqual([], payload['mailboxes'])


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_retries_windows_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            real_replace = os.replace

            def replace_twice(source, destination):
                if not AtomicWriteTests.replace_calls:
                    AtomicWriteTests.replace_calls.append(True)
                    raise OSError(17, 'transient Windows replace failure')
                return real_replace(source, destination)

            AtomicWriteTests.replace_calls = []
            with mock.patch.object(
                os, 'replace', side_effect=replace_twice
            ):
                mail_digest._atomic_write_text(path, '{"ok":true}')

            self.assertEqual(1, len(AtomicWriteTests.replace_calls))
            self.assertEqual('{"ok":true}', path.read_text(encoding='utf-8'))
            self.assertEqual([], list(Path(directory).glob('*.tmp')))


class DismissedMailTests(unittest.TestCase):
    def test_filter_dismissed_html_removes_only_matching_card(self):
        key_a = 'a' * 40
        key_b = 'b' * 40
        html = (
            f'<article class="mail" data-key="{key_a}">one</article>'
            f'<article class="mail" data-key="{key_b}">two</article>'
        )

        filtered = mail_digest.filter_dismissed_html(html, {key_a})

        self.assertNotIn('>one</article>', filtered)
        self.assertIn('>two</article>', filtered)

    def test_latest_digest_is_atomically_updated_after_dismiss(self):
        with tempfile.TemporaryDirectory() as directory:
            digest_dir = Path(directory)
            key = 'c' * 40
            path = digest_dir / '2026-09-02.html'
            path.write_text(
                f'<article class="mail" data-key="{key}">mail</article>',
                encoding='utf-8',
            )

            removed = mail_digest.remove_dismissed_from_latest_digest(
                {key}, digest_dir
            )

            self.assertEqual(1, removed)
            self.assertNotIn('class="mail"', path.read_text(encoding='utf-8'))

    def test_dismiss_store_is_deduplicated_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dismissed-mail.json'
            now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
            added = mail_digest.dismiss_mail_keys(
                ['key-a', ' key-a ', 'key-b'],
                now=now,
                store_path=path,
            )

            self.assertEqual(2, added)
            payload = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual({'key-a', 'key-b'}, set(payload))

            added = mail_digest.dismiss_mail_keys(
                ['key-b'], now=now, store_path=path
            )

        self.assertEqual(0, added)

    def test_dismiss_fails_explicitly_when_store_cannot_be_saved(self):
        with mock.patch.object(
            mail_digest, 'save_dismissed_store', return_value=False
        ):
            with self.assertRaises(OSError):
                mail_digest.dismiss_mail_keys(['key-a'])

    def test_dismissed_store_prunes_invalid_and_old_entries(self):
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        store = {
            'fresh': now.isoformat(),
            'stale': (now - timedelta(days=31)).isoformat(),
            'invalid': 'not-a-date',
        }
        kept = mail_digest._prune_dismissed(store, now)

        self.assertEqual({'fresh'}, set(kept))

    def test_apply_dismissed_filter_removes_only_matching_cards(self):
        box = MailboxDigest(
            mailbox_id='qq_mail',
            display_name='QQ 邮箱',
            short_name='QQ',
            status='READY',
            message='',
            emails=[
                DigestMail('a', 'kept', '', body_text='keep body'),
                DigestMail('b', 'hidden', '', body_text='hidden body'),
            ],
        )
        dismissed = {
            mail_digest._mail_cache_key('qq_mail', box.emails[1])
        }

        with mock.patch.object(
            mail_digest, 'dismissed_keys', return_value=dismissed
        ):
            removed = mail_digest._apply_dismissed_filter([box])

        self.assertEqual(1, removed)
        self.assertEqual('kept', box.emails[0].subject)
        self.assertEqual(1, len(box.emails))

    def test_stable_reference_stays_dismissed_when_body_changes(self):
        original = DigestMail(
            'sender', 'subject', '', body_text='old body',
            source_reference='imap:42',
        )
        refreshed = DigestMail(
            'sender', 'subject', '', body_text='new body',
            source_reference='imap:42',
        )
        dismissed = {mail_digest._mail_dismiss_key('qq_mail', original)}
        box = MailboxDigest(
            'qq_mail', 'QQ 邮箱', 'QQ', 'READY', '', [refreshed]
        )

        with mock.patch.object(
            mail_digest, 'dismissed_keys', return_value=dismissed
        ):
            removed = mail_digest._apply_dismissed_filter([box])

        self.assertEqual(1, removed)
        self.assertEqual([], box.emails)


class MailBodyTextTest(unittest.TestCase):
    def test_enrich_persists_cache_once_after_batch(self) -> None:
        box = MailboxDigest(
            mailbox_id='qq_mail',
            display_name='QQ 邮箱',
            short_name='QQ',
            status='READY',
            message='',
            emails=[
                DigestMail('a', 's1', '', body_text='正文一'),
                DigestMail('b', 's2', '', body_text='正文二'),
            ],
        )

        def fake_post(url, headers=None, json=None, timeout=None):
            user_content = json['messages'][1]['content']
            content = (
                '{"summary": "中文摘要", "importance": "中"}'
                if 'JSON' in user_content else '中文翻译'
            )
            return SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {'content': content}}]},
            )

        with mock.patch.object(
            mail_digest, 'load_translation_cache', return_value={}
        ), mock.patch.object(mail_digest, 'save_translation_cache') as save:
            enrich_digests([box], 'test-key', transport=fake_post)

        save.assert_called_once()

    def test_strip_html_removes_styles_scripts_and_escapes_entities(self) -> None:
        html_text = '<style>.x{}</style><p>你好 &amp; <b>世界</b></p><script>bad()</script>'
        self.assertEqual(strip_html(html_text), '你好 & 世界')

    def test_clean_body_cuts_quoted_reply_and_caps_length(self) -> None:
        cleaned = clean_body_text('报告要点：尽快提交 ----- De : someone')
        self.assertEqual(cleaned, '报告要点：尽快提交')
        self.assertLessEqual(len(clean_body_text('x' * 5000)), 3500)

    def test_extract_body_text_prefers_plain_and_skips_attachments(self) -> None:
        message = EmailMessage()
        message.set_content('正文内容在这里')
        message.add_attachment(
            b'zz', maintype='application', subtype='pdf', filename='a.pdf'
        )
        self.assertEqual(extract_body_text(message), '正文内容在这里')

    def test_extract_attachment_metadata_does_not_decode_payload(self) -> None:
        message = EmailMessage()
        message.set_content('正文内容在这里')
        message.add_attachment(
            b'12345',
            maintype='application',
            subtype='pdf',
            filename='课程说明.pdf',
        )
        message.add_attachment(
            b'logo', maintype='image', subtype='png', filename='logo.png'
        )
        inline_part = message.get_payload()[-1]
        inline_part.replace_header(
            'Content-Disposition', 'inline; filename="logo.png"'
        )

        attachments = extract_attachment_metadata(message)

        self.assertEqual(1, len(attachments))
        self.assertEqual('课程说明.pdf', attachments[0].name)
        self.assertEqual('application/pdf', attachments[0].content_type)
        self.assertEqual(5, attachments[0].size_bytes)

    def test_graph_attachment_metadata_requests_only_safe_fields(self) -> None:
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured['url'] = url
            return SimpleNamespace(
                status_code=200,
                json=lambda: {'value': [
                    {
                        'name': 'report.pdf',
                        'contentType': 'application/pdf',
                        'size': 1536,
                        'isInline': False,
                        'contentBytes': 'must-not-be-used',
                    },
                    {
                        'name': 'logo.png',
                        'contentType': 'image/png',
                        'size': 100,
                        'isInline': True,
                    },
                ]},
            )

        attachments = mail_digest._graph_attachment_metadata(
            'message/id', 'token', transport=fake_get
        )

        self.assertIn('%24select=name%2CcontentType%2Csize%2CisInline', captured['url'])
        self.assertNotIn('contentBytes', captured['url'])
        self.assertEqual(['report.pdf'], [item.name for item in attachments])
        self.assertEqual(1536, attachments[0].size_bytes)

    def test_graph_body_text_strips_html(self) -> None:
        body = {'contentType': 'HTML', 'content': '<p>Salut &amp; à demain</p>'}
        self.assertEqual(graph_body_text(body), 'Salut & à demain')

    def test_build_summary_prompt_requests_chinese(self) -> None:
        prompt = build_full_ai_prompt(DigestMail('a', '主题x', '', body_text='内容y'))
        self.assertIn('主题x', prompt)
        self.assertIn('内容y', prompt)
        self.assertIn('简体中文', prompt)
        self.assertIn('JSON', prompt)
        self.assertIn('importance', prompt)
        self.assertIn('需要收件人采取行动为高', prompt)
        self.assertIn('学校相关但无需行动', prompt)
        self.assertIn('其他所有邮件为低', prompt)

    def test_enrich_digests_calls_api_and_skips_empty_bodies(self) -> None:
        box = MailboxDigest(
            mailbox_id='qq_mail',
            display_name='QQ 邮箱',
            short_name='QQ',
            status='READY',
            message='',
            emails=[
                DigestMail('a', 's1', '', body_text='第一条正文'),
                DigestMail('b', 's2', '', body_text=''),
            ],
        )
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            user_content = json['messages'][1]['content']
            calls.append(user_content)
            if 'JSON' in user_content:
                content = '{"summary": " 这是摘要 ", "importance": "高"}'
            else:
                content = '翻译后的中文内容'
            return SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {'content': content}}]},
            )

        enrich_digests([box], 'test-key', transport=fake_post, cache={})
        self.assertEqual(box.emails[0].summary, '这是摘要')
        self.assertEqual(box.emails[0].importance, '高')
        self.assertEqual(box.emails[0].translation, '翻译后的中文内容')
        self.assertEqual(box.emails[1].summary, '')
        self.assertEqual(box.emails[1].translation, '')
        self.assertEqual(len(calls), 2)

    def test_enrich_digests_reuses_cache_without_new_calls(self) -> None:
        cache = {
            hashlib.sha256('qq_mail|s1|正文'.encode('utf-8')).hexdigest()[:40]:
            {
                'summary': '缓存摘要',
                'translation': '缓存翻译',
                'classification_policy_version': (
                    mail_digest.CLASSIFICATION_POLICY_VERSION
                ),
            },
        }
        box = MailboxDigest(
            mailbox_id='qq_mail',
            display_name='QQ 邮箱',
            short_name='QQ',
            status='READY',
            message='',
            emails=[DigestMail('a', 's1', '', body_text='正文')],
        )

        def no_post(url, headers=None, json=None, timeout=None):
            raise AssertionError('should not call API when cache hits')

        enrich_digests([box], 'test-key', transport=no_post, cache=cache)
        self.assertEqual(box.emails[0].summary, '缓存摘要')
        self.assertEqual(box.emails[0].translation, '缓存翻译')

    def test_old_policy_cache_reclassifies_without_retranslation(self) -> None:
        cache_key = hashlib.sha256(
            'qq_mail|s1|正文'.encode('utf-8')
        ).hexdigest()[:40]
        cache = {
            cache_key: {
                'summary': '旧缓存摘要',
                'translation': '旧缓存翻译',
                'importance': '高',
            },
        }
        box = MailboxDigest(
            mailbox_id='qq_mail',
            display_name='QQ 邮箱',
            short_name='QQ',
            status='READY',
            message='',
            emails=[DigestMail('a', 's1', '', body_text='正文')],
        )
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json['messages'][1]['content'])
            return SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {'content': (
                    '{"summary": "新摘要", "importance": "低"}'
                )}}]},
            )

        enrich_digests([box], 'test-key', transport=fake_post, cache=cache)

        self.assertEqual(1, len(calls))
        self.assertEqual('旧缓存翻译', box.emails[0].translation)
        self.assertEqual('低', box.emails[0].importance)
        self.assertEqual(
            mail_digest.CLASSIFICATION_POLICY_VERSION,
            cache[cache_key]['classification_policy_version'],
        )

    def test_enrich_digests_falls_back_to_preview_on_api_error(self) -> None:
        box = MailboxDigest(
            mailbox_id='qq_mail',
            display_name='QQ 邮箱',
            short_name='QQ',
            status='READY',
            message='',
            emails=[DigestMail('a', 's1', '', body_text='正文预览测试内容')],
        )

        def bad_post(url, headers=None, json=None, timeout=None):
            raise requests.ConnectionError('down')

        enrich_digests([box], 'test-key', transport=bad_post, cache={})
        self.assertTrue(box.emails[0].summary.startswith('正文预览'))
        self.assertEqual(box.emails[0].translation, '')
        self.assertIn('正文预览测试内容', box.emails[0].summary)

    def test_enrich_retries_when_translation_is_not_chinese(self) -> None:
        box = MailboxDigest(
            mailbox_id='bachelor_mail',
            display_name='传媒大学本科邮箱',
            short_name='本科',
            status='READY',
            message='',
            emails=[DigestMail('a', 's1', '', body_text='Bonjour le monde')],
        )
        responses = [
            'Bonjour le monde',
            '你好，世界。请尽快提交报告。',
        ]
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            user_content = json['messages'][1]['content']
            calls.append(user_content)
            if 'JSON' in user_content:
                content = '{"summary": "报告提醒"}'
            else:
                content = responses.pop(0)
            return SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {'content': content}}]},
            )

        enrich_digests([box], 'test-key', transport=fake_post, cache={})
        self.assertEqual(len(calls), 3)
        self.assertIn('上一次输出不是简体中文', calls[2])
        self.assertEqual(box.emails[0].summary, '报告提醒')
        self.assertIn('请尽快提交报告', box.emails[0].translation)

    def test_call_mail_translation_raises_when_never_chinese(self) -> None:
        def french_post(url, headers=None, json=None, timeout=None):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {
                    'content': 'Bonjour'
                }}]},
            )

        with self.assertRaises(SummaryAPIError):
            call_mail_translation('Bonjour le monde', 'test-key', transport=french_post)

    def test_enrich_ignores_cache_without_chinese_translation(self) -> None:
        cache_key = hashlib.sha256(
            'qq_mail|s1|Bonjour le monde'.encode('utf-8')
        ).hexdigest()[:40]
        cache = {cache_key: {'summary': 'Résumé', 'translation': 'Bonjour le monde'}}
        box = MailboxDigest(
            mailbox_id='qq_mail',
            display_name='QQ 邮箱',
            short_name='QQ',
            status='READY',
            message='',
            emails=[DigestMail('a', 's1', '', body_text='Bonjour le monde')],
        )
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            user_content = json['messages'][1]['content']
            calls.append(user_content)
            if 'JSON' in user_content:
                content = '{"summary": "中文摘要"}'
            else:
                content = '中文翻译'
            return SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {'content': content}}]},
            )

        enrich_digests([box], 'test-key', transport=fake_post, cache=cache)
        self.assertEqual(len(calls), 2)
        self.assertEqual(box.emails[0].summary, '中文摘要')
        self.assertEqual(box.emails[0].translation, '中文翻译')

    def test_select_new_high_only_returns_unnotified_high(self) -> None:
        box = MailboxDigest(
            mailbox_id='qq_mail',
            display_name='QQ 邮箱',
            short_name='QQ',
            status='READY',
            message='',
            emails=[
                _mail('a', '重要提醒', '', importance='高'),
                _mail('b', '普通通知', '', importance='中'),
            ],
        )
        fresh = select_new_high([box], notified=set())
        self.assertEqual(len(fresh), 1)
        key = _mail_cache_key('qq_mail', fresh[0])
        again = select_new_high([box], notified={key})
        self.assertEqual(again, [])

    def test_prune_notified_drops_stale_entries(self) -> None:
        now = datetime(2026, 8, 29, 12, 0)
        store = {
            'fresh': now.isoformat(),
            'stale': '2026-08-01T00:00:00+00:00',
            'broken': 'not-a-date',
        }
        kept = _prune_notified(store, now, max_age_days=7)
        self.assertIn('fresh', kept)
        self.assertNotIn('stale', kept)
        self.assertNotIn('broken', kept)


if __name__ == '__main__':
    unittest.main()
