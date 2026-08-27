"""Side-effect-free tests for fixed mailbox identities and Edge launching."""

import unittest
from unittest.mock import call, patch

from windows_gui.mailboxes import (
    MAILBOX_IDENTITIES,
    _clear_runtime_mailbox_contexts,
    _profile_directory_from_command_line,
    confirm_mailbox_identity,
    get_or_open_mailbox_window,
    get_runtime_mailbox_context,
    open_all_mailboxes,
)
from windows_gui import mailboxes


class MailboxConfigurationTests(unittest.TestCase):
    def test_edge_window_detection_ignores_zero_width_title_characters(self):
        def enumerate_windows(callback, extra):
            callback(77, extra)

        with (
            patch.object(mailboxes.win32gui, "EnumWindows", side_effect=enumerate_windows),
            patch.object(mailboxes.win32gui, "IsWindowVisible", return_value=True),
            patch.object(
                mailboxes.win32gui,
                "GetWindowText",
                return_value="Inbox - Microsoft\u200b Edge",
            ),
        ):
            self.assertEqual({77: "Inbox - Microsoft\u200b Edge"}, mailboxes._visible_edge_windows())

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
        self.assertEqual(
            ("outlook.cloud.microsoft",), master.service_domain_aliases
        )
        self.assertEqual("Profile 3", qq.profile_directory)
        self.assertEqual(("READ", "DRAFT"), qq.permissions)
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
    @patch("windows_gui.mailboxes._find_existing_profile_window", return_value=None)
    @patch("windows_gui.mailboxes.subprocess.Popen")
    @patch("windows_gui.mailboxes._find_edge_executable", return_value="msedge.exe")
    def test_opens_three_separate_profile_windows(
        self, find_edge, popen, find_existing, wait_for_window
    ):
        results = open_all_mailboxes()

        self.assertEqual(3, find_edge.call_count)
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
                "CREATED_NEW_WINDOW",
                "CREATED_NEW_WINDOW",
                "CREATED_NEW_WINDOW",
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
    @patch("windows_gui.mailboxes._find_existing_profile_window", return_value=None)
    @patch("windows_gui.mailboxes.subprocess.Popen")
    @patch("windows_gui.mailboxes._find_edge_executable", return_value="msedge.exe")
    def test_one_launch_failure_does_not_hide_other_statuses(
        self, find_edge, popen, find_existing, wait_for_window
    ):
        popen.side_effect = [OSError("blocked"), None, None]
        results = open_all_mailboxes()

        self.assertEqual("failed", results[0]["status"])
        self.assertEqual("blocked", results[0]["error"])
        self.assertEqual("CREATED_NEW_WINDOW", results[1]["status"])
        self.assertEqual("CREATED_NEW_WINDOW", results[2]["status"])

    @patch("windows_gui.mailboxes._focus_window_handle")
    @patch("windows_gui.mailboxes._read_edge_service_domain", return_value="mail.qq.com")
    @patch("windows_gui.mailboxes._context_window_is_valid", side_effect=[False, True])
    @patch("windows_gui.mailboxes._find_existing_profile_window", return_value=None)
    @patch("windows_gui.mailboxes._wait_for_launched_edge_window", return_value=301)
    @patch("windows_gui.mailboxes.subprocess.Popen")
    @patch("windows_gui.mailboxes._find_edge_executable", return_value="msedge.exe")
    def test_consecutive_calls_create_only_one_window(
        self, find_edge, popen, wait_for_window, find_existing,
        context_valid, read_domain, focus
    ):
        first = get_or_open_mailbox_window("qq_mail")
        second = get_or_open_mailbox_window("qq_mail")
        self.assertEqual("CREATED_NEW_WINDOW", first["status"])
        self.assertEqual("REUSED_EXISTING_WINDOW", second["status"])
        popen.assert_called_once()
        focus.assert_called_once_with(301)

    @patch("windows_gui.mailboxes._focus_window_handle")
    @patch("windows_gui.mailboxes._find_existing_profile_window", return_value=401)
    @patch("windows_gui.mailboxes.subprocess.Popen")
    def test_missing_runtime_context_restores_existing_profile_window(
        self, popen, find_existing, focus
    ):
        result = get_or_open_mailbox_window("master_mail")
        self.assertEqual("RESTORED_WINDOW_BINDING", result["status"])
        self.assertEqual(401, get_runtime_mailbox_context("master_mail").hwnd)
        popen.assert_not_called()
        focus.assert_called_once_with(401)

    def test_profile_parser_does_not_retain_url_or_sid(self):
        command = (
            'msedge.exe --profile-directory="Profile 1" '
            'https://mailh.qiye.163.com/path?sid=do-not-retain'
        )
        result = _profile_directory_from_command_line(command)
        self.assertEqual("Profile 1", result)
        self.assertNotIn("sid", result.casefold())

    @patch("windows_gui.mailboxes._read_edge_service_domain", return_value=None)
    @patch("windows_gui.mailboxes._profile_directory_for_hwnd", return_value=None)
    @patch(
        "windows_gui.mailboxes._visible_edge_windows",
        return_value={501: "新建标签页 - 本科邮箱 - Microsoft\u200b Edge"},
    )
    def test_shared_edge_process_restores_from_exact_profile_title_suffix(
        self, visible, profile_for_hwnd, read_domain
    ):
        self.assertEqual(
            501,
            mailboxes._find_existing_profile_window(
                MAILBOX_IDENTITIES["bachelor_mail"]
            ),
        )


if __name__ == "__main__":
    unittest.main()
