"""Side-effect-free tests for fixed mailbox identities and Edge launching."""

import unittest
from unittest.mock import call, patch

from windows_gui.mailboxes import (
    MAILBOX_IDENTITIES,
    _clear_runtime_mailbox_contexts,
    confirm_mailbox_identity,
    get_runtime_mailbox_context,
    open_all_mailboxes,
)


class MailboxConfigurationTests(unittest.TestCase):
    def test_fixed_mailbox_configuration_contains_no_credentials(self):
        self.assertEqual(
            {"bachelor_mail", "master_mail", "qq_mail"},
            set(MAILBOX_IDENTITIES),
        )
        serialized = repr(MAILBOX_IDENTITIES).lower()
        for forbidden in ("password", "cookie", "sid", "token"):
            self.assertNotIn(forbidden, serialized)

    def test_profiles_permissions_and_urls_are_fixed(self):
        bachelor = MAILBOX_IDENTITIES["bachelor_mail"]
        master = MAILBOX_IDENTITIES["master_mail"]
        qq = MAILBOX_IDENTITIES["qq_mail"]

        self.assertEqual("Profile 1", bachelor.profile_directory)
        self.assertEqual(("READ", "DRAFT", "SEND"), bachelor.permissions)
        self.assertEqual("mailh.qiye.163.com", bachelor.service_domain)
        self.assertIsNone(bachelor.stable_url)
        self.assertEqual("Profile 2", master.profile_directory)
        self.assertEqual("https://outlook.office.com/mail/", master.stable_url)
        self.assertEqual("outlook.office.com", master.service_domain)
        self.assertEqual("Profile 3", qq.profile_directory)
        self.assertEqual(("READ",), qq.permissions)
        self.assertEqual("https://mail.qq.com/", qq.stable_url)
        self.assertEqual("mail.qq.com", qq.service_domain)
        self.assertTrue(bachelor.send_requires_confirmation)
        self.assertTrue(master.send_requires_confirmation)
        self.assertTrue(qq.send_requires_confirmation)

    def test_identity_confirmation_rejects_unknown_or_wrong_profile(self):
        with self.assertRaisesRegex(ValueError, "Unknown mailbox"):
            confirm_mailbox_identity("missing", "Profile 1")
        with self.assertRaisesRegex(ValueError, "Profile mismatch"):
            confirm_mailbox_identity("master_mail", "Profile 1")


class OpenAllMailboxesTests(unittest.TestCase):
    def setUp(self):
        _clear_runtime_mailbox_contexts()

    @patch(
        "windows_gui.mailboxes._wait_for_launched_edge_window",
        side_effect=[101, 102, 103],
    )
    @patch("windows_gui.mailboxes.subprocess.Popen")
    @patch("windows_gui.mailboxes._find_edge_executable", return_value="msedge.exe")
    def test_opens_three_separate_profile_windows(
        self, find_edge, popen, wait_for_window
    ):
        results = open_all_mailboxes()

        find_edge.assert_called_once_with()
        self.assertEqual(3, popen.call_count)
        self.assertEqual([
            call([
                "msedge.exe", "--profile-directory=Profile 1", "--new-window",
            ], close_fds=True),
            call([
                "msedge.exe", "--profile-directory=Profile 2", "--new-window",
                "https://outlook.office.com/mail/",
            ], close_fds=True),
            call([
                "msedge.exe", "--profile-directory=Profile 3", "--new-window",
                "https://mail.qq.com/",
            ], close_fds=True),
        ], popen.call_args_list)
        self.assertEqual(
            [
                "profile_opened_mailbox_url_not_configured",
                "opened",
                "opened",
            ],
            [result["status"] for result in results],
        )
        self.assertEqual(
            ("Profile 1", 101),
            (
                get_runtime_mailbox_context("bachelor_mail").profile_directory,
                get_runtime_mailbox_context("bachelor_mail").hwnd,
            ),
        )
        self.assertEqual(
            ("Profile 2", 102),
            (
                get_runtime_mailbox_context("master_mail").profile_directory,
                get_runtime_mailbox_context("master_mail").hwnd,
            ),
        )
        self.assertEqual(
            ("Profile 3", 103),
            (
                get_runtime_mailbox_context("qq_mail").profile_directory,
                get_runtime_mailbox_context("qq_mail").hwnd,
            ),
        )

    @patch(
        "windows_gui.mailboxes._wait_for_launched_edge_window",
        side_effect=[201, 202],
    )
    @patch("windows_gui.mailboxes.subprocess.Popen")
    @patch("windows_gui.mailboxes._find_edge_executable", return_value="msedge.exe")
    def test_one_launch_failure_does_not_hide_other_statuses(
        self, find_edge, popen, wait_for_window
    ):
        popen.side_effect = [OSError("blocked"), None, None]
        results = open_all_mailboxes()

        self.assertEqual("failed", results[0]["status"])
        self.assertEqual("blocked", results[0]["error"])
        self.assertEqual("opened", results[1]["status"])
        self.assertEqual("opened", results[2]["status"])


if __name__ == "__main__":
    unittest.main()
