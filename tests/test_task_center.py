"""Unit tests for the internal task and confirmation center."""

import itertools
import threading
import unittest

import windows_gui.mail_assistant as mail_assistant
from windows_gui.mail_assistant import AssistantError
from windows_gui.task_center import (
    STATE_CANCELLED,
    STATE_EXECUTING,
    STATE_EXPIRED,
    STATE_FAILED,
    STATE_STAGED,
    STATE_SUCCEEDED,
    TaskCenter,
    TaskCenterError,
    TaskConsumedError,
    TaskExpiredError,
    UnknownActionTypeError,
    UnknownDomainError,
    UnknownTaskError,
)


def make_center(**overrides) -> TaskCenter:
    counter = itertools.count()
    defaults = dict(
        domains=('mail',),
        action_types=('assistant_send_draft',),
        now_factory=lambda: 1_000.0,
        id_factory=lambda: f'fixed-task-id-{next(counter)}',
    )
    defaults.update(overrides)
    return TaskCenter(**defaults)


class TaskCenterLifecycleTest(unittest.TestCase):
    def test_stage_and_consume_round_trip(self) -> None:
        center = make_center()
        task_id = center.stage(
            'mail', 'assistant_send_draft', {'secret': 'body-text'},
        )
        view = center.lookup(task_id)
        self.assertEqual(view.state, STATE_STAGED)
        self.assertEqual(view.domain, 'mail')
        self.assertEqual(view.action_type, 'assistant_send_draft')
        context = center.consume(task_id)
        self.assertEqual(context, {'secret': 'body-text', '_expires_at_mono': 1900.0})
        self.assertEqual(center.lookup(task_id).state, STATE_EXECUTING)
        center.complete(task_id, success=True)
        self.assertEqual(center.lookup(task_id).state, STATE_SUCCEEDED)

    def test_consume_is_single_use(self) -> None:
        center = make_center()
        task_id = center.stage('mail', 'assistant_send_draft', {'n': 1})
        center.consume(task_id)
        with self.assertRaises(TaskConsumedError):
            center.consume(task_id)

    def test_expired_reference_fails_explicitly(self) -> None:
        clock = {'now': 100.0}
        center = make_center(now_factory=lambda: clock['now'])
        task_id = center.stage(
            'mail', 'assistant_send_draft', {'n': 1}, ttl_seconds=60,
        )
        clock['now'] = 160.0
        with self.assertRaises(TaskExpiredError):
            center.consume(task_id)
        self.assertEqual(center.lookup(task_id).state, STATE_EXPIRED)
        self.assertNotIn(task_id, center.pending)

    def test_purge_expired_removes_due_tasks(self) -> None:
        clock = {'now': 100.0}
        center = make_center(now_factory=lambda: clock['now'])
        center.stage('mail', 'assistant_send_draft', {'n': 1}, ttl_seconds=60)
        clock['now'] = 159.0
        self.assertEqual(center.purge_expired(), 0)
        self.assertEqual(center.pending_count(), 1)
        clock['now'] = 160.0
        self.assertEqual(center.purge_expired(), 1)
        self.assertEqual(center.pending_count(), 0)

    def test_capacity_evicts_oldest_staged_task(self) -> None:
        center = make_center(max_tasks=2)
        first = center.stage('mail', 'assistant_send_draft', {'n': 1})
        second = center.stage('mail', 'assistant_send_draft', {'n': 2})
        third = center.stage('mail', 'assistant_send_draft', {'n': 3})
        self.assertNotIn(first, center.pending)
        self.assertIn(second, center.pending)
        self.assertIn(third, center.pending)
        self.assertEqual(center.pending_count(), 2)
        with self.assertRaises(UnknownTaskError):
            center.consume(first)

    def test_unknown_reference_fails_explicitly(self) -> None:
        center = make_center()
        with self.assertRaises(UnknownTaskError):
            center.consume('never-staged')
        self.assertIsNone(center.lookup('never-staged'))

    def test_rejects_unknown_domain(self) -> None:
        center = make_center()
        with self.assertRaises(UnknownDomainError):
            center.stage('browser', 'assistant_send_draft', {})

    def test_rejects_unknown_action_type(self) -> None:
        center = make_center()
        with self.assertRaises(UnknownActionTypeError):
            center.stage('mail', 'unknown_action', {})

    def test_rejects_invalid_ttl_and_capacity(self) -> None:
        center = make_center()
        with self.assertRaises(ValueError):
            center.stage(
                'mail', 'assistant_send_draft', {}, ttl_seconds=0,
            )
        with self.assertRaises(ValueError):
            center.stage(
                'mail', 'assistant_send_draft', {}, max_items=0,
            )

    def test_verified_context_never_enters_public_views(self) -> None:
        center = make_center()
        task_id = center.stage('mail', 'assistant_send_draft', {
            'body': 'TOPSECRET-BODY',
            'token': 'TOPSECRET-TOKEN',
        })
        view = center.lookup(task_id)
        public = view.to_public_dict()
        self.assertEqual(
            set(public),
            {'task_id', 'domain', 'action_type', 'state'},
        )
        self.assertNotIn('TOPSECRET-BODY', str(public))
        self.assertNotIn('TOPSECRET-TOKEN', repr(view))
        self.assertNotIn('TOPSECRET-BODY', repr(center))

    def test_cancelled_task_cannot_be_consumed(self) -> None:
        center = make_center()
        task_id = center.stage('mail', 'assistant_send_draft', {'n': 1})
        self.assertTrue(center.cancel(task_id))
        self.assertEqual(center.lookup(task_id).state, STATE_CANCELLED)
        self.assertNotIn(task_id, center.pending)
        with self.assertRaises(TaskConsumedError):
            center.consume(task_id)
        self.assertFalse(center.cancel(task_id))

    def test_failed_outcome_recorded_once(self) -> None:
        center = make_center()
        task_id = center.stage('mail', 'assistant_send_draft', {'n': 1})
        center.consume(task_id)
        center.complete(task_id, success=False)
        self.assertEqual(center.lookup(task_id).state, STATE_FAILED)
        with self.assertRaises(UnknownTaskError):
            center.complete(task_id, success=True)


class TaskCenterConcurrencyTest(unittest.TestCase):
    def test_concurrent_consumes_allow_exactly_one_winner(self) -> None:
        center = make_center(max_tasks=4)
        task_ids = [
            center.stage('mail', 'assistant_send_draft', {'n': index})
            for index in range(4)
        ]
        winners: list[str] = []
        losers: list[TaskCenterError] = []
        guard = threading.Lock()

        def try_consume(task_id: str) -> None:
            try:
                center.consume(task_id)
            except TaskCenterError as error:
                with guard:
                    losers.append(error)
            else:
                with guard:
                    winners.append(task_id)

        threads = [
            threading.Thread(target=try_consume, args=(task_id,))
            for task_id in task_ids * 4
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(winners), 4)
        self.assertEqual(len(losers), 12)
        self.assertEqual(sorted(winners), sorted(task_ids))
        self.assertEqual(center.pending_count(), 0)

    def test_concurrent_staging_respects_capacity_and_unique_ids(self) -> None:
        center = make_center(max_tasks=4)
        staged: list[str] = []
        guard = threading.Lock()

        def stage(index: int) -> None:
            task_id = center.stage(
                'mail', 'assistant_send_draft', {'n': index},
            )
            with guard:
                staged.append(task_id)

        threads = [
            threading.Thread(target=stage, args=(index,))
            for index in range(16)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(staged), 16)
        self.assertEqual(len(set(staged)), 16)
        self.assertLessEqual(center.pending_count(), 4)


class MailAssistantMigrationTest(unittest.TestCase):
    def tearDown(self) -> None:
        mail_assistant.PENDING_DRAFTS.clear()

    def test_defaults_match_pending_draft_contract(self) -> None:
        self.assertEqual(mail_assistant.PENDING_DRAFT_TTL_SECONDS, 15 * 60)
        self.assertEqual(mail_assistant.MAX_PENDING_DRAFTS, 16)

    def test_store_and_take_round_trip(self) -> None:
        pending_id = mail_assistant._store_pending_draft(
            {'mailbox_id': 'master_mail'},
        )
        self.assertIn(pending_id, mail_assistant.PENDING_DRAFTS)
        context = mail_assistant._take_pending_draft(pending_id)
        self.assertEqual(context['mailbox_id'], 'master_mail')
        self.assertNotIn(pending_id, mail_assistant.PENDING_DRAFTS)

    def test_consumed_reference_fails_with_stable_message(self) -> None:
        pending_id = mail_assistant._store_pending_draft(
            {'mailbox_id': 'master_mail'},
        )
        mail_assistant._take_pending_draft(pending_id)
        with self.assertRaises(AssistantError) as caught:
            mail_assistant._take_pending_draft(pending_id)
        self.assertEqual(
            str(caught.exception),
            '待发送草稿不存在、已确认或服务已重启',
        )

    def test_send_failure_records_failed_outcome(self) -> None:
        pending_id = mail_assistant._store_pending_draft(
            {'mailbox_id': 'unknown_mailbox'},
        )
        with self.assertRaises(AssistantError):
            mail_assistant.send_staged_draft(pending_id)
        view = mail_assistant._TASK_CENTER.lookup(pending_id)
        self.assertEqual(view.state, STATE_FAILED)


if __name__ == '__main__':
    unittest.main()
