"""Side-effect-free tests for the unified mailbox summary tool."""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from windows_gui.mail_summary import (
    _extract_today_emails,
    _find_verified_snapshot,
    _is_sensitive_browser_control,
    _service_domain_matches,
    _summarize_mailbox,
    summarize_all_mailboxes_today,
)
from windows_gui.mailboxes import MAILBOX_IDENTITIES


class MailSummaryTests(unittest.TestCase):
    def test_address_bar_and_edit_controls_are_never_retained(self):
        self.assertTrue(_is_sensitive_browser_control(
            "Address and search bar", "Edit", "view_1022"
        ))
        self.assertTrue(_is_sensitive_browser_control(
            "private address field contents", "Edit", "address-bar"
        ))
        self.assertFalse(_is_sensitive_browser_control(
            "Sender | Subject | 10:00", "ListItem", "mail-row"
        ))

    def test_profile_identity_mapping_and_stable_urls(self):
        bachelor = MAILBOX_IDENTITIES["bachelor_mail"]
        master = MAILBOX_IDENTITIES["master_mail"]
        qq = MAILBOX_IDENTITIES["qq_mail"]
        self.assertEqual("Profile 1", bachelor.profile_directory)
        self.assertIsNone(bachelor.stable_url)
        self.assertEqual("Profile 2", master.profile_directory)
        self.assertEqual("https://outlook.office.com/mail/", master.stable_url)
        self.assertEqual("Profile 3", qq.profile_directory)
        self.assertEqual("https://mail.qq.com/", qq.stable_url)

    def test_permission_restrictions_remain_read_only_for_qq(self):
        self.assertEqual(("READ",), MAILBOX_IDENTITIES["qq_mail"].permissions)
        self.assertNotIn("DRAFT", MAILBOX_IDENTITIES["qq_mail"].permissions)
        self.assertNotIn("SEND", MAILBOX_IDENTITIES["qq_mail"].permissions)
        self.assertTrue(MAILBOX_IDENTITIES["master_mail"].send_requires_confirmation)

    @patch("windows_gui.mail_summary._ensure_mailbox_page")
    def test_bachelor_not_ready_path(self, ensure):
        ensure.return_value = (None, "PAGE_NOT_READY")
        result = _summarize_mailbox(MAILBOX_IDENTITIES["bachelor_mail"])
        self.assertEqual("NOT_READY", result["status"])
        self.assertEqual(
            "本科邮箱页面未就绪，需要人工打开邮箱页面", result["message"]
        )
        self.assertEqual([], result["emails"])

    @patch("windows_gui.mail_summary._ensure_mailbox_page")
    def test_unconfirmed_identity_stops_without_reading(self, ensure):
        ensure.return_value = (None, "UNKNOWN_WINDOW")
        result = _summarize_mailbox(MAILBOX_IDENTITIES["master_mail"])
        self.assertEqual("NOT_READY", result["status"])
        self.assertRegex(result["message"], r"无法.*(?:确认|验证)")

    def test_service_domains_require_exact_match(self):
        self.assertTrue(
            _service_domain_matches("outlook.office.com", "outlook.office.com")
        )
        self.assertFalse(
            _service_domain_matches("outlook.office.com", "evil-outlook.office.com")
        )
        self.assertTrue(_service_domain_matches("mail.qq.com", "mail.qq.com"))
        self.assertTrue(
            _service_domain_matches("mailh.qiye.163.com", "mailh.qiye.163.com")
        )

    @patch("windows_gui.mail_summary._snapshot_edge_window")
    @patch("windows_gui.mail_summary.get_runtime_mailbox_context")
    def test_runtime_profile_and_service_domain_produce_ready(
        self, get_context, snapshot
    ):
        get_context.return_value = SimpleNamespace(
            profile_directory="Profile 2", hwnd=88
        )
        snapshot.return_value = {
            "service_domain": "outlook.office.com", "controls": []
        }
        result, state = _find_verified_snapshot(
            MAILBOX_IDENTITIES["master_mail"]
        )
        self.assertEqual("READY", state)
        self.assertEqual(snapshot.return_value, result)
        snapshot.assert_called_once_with(88)

    @patch("windows_gui.mail_summary._snapshot_edge_window")
    @patch("windows_gui.mail_summary.get_runtime_mailbox_context")
    def test_bachelor_existing_netease_page_is_ready(
        self, get_context, snapshot
    ):
        get_context.return_value = SimpleNamespace(
            profile_directory="Profile 1", hwnd=81
        )
        snapshot.return_value = {
            "service_domain": "mailh.qiye.163.com", "controls": []
        }
        _, state = _find_verified_snapshot(
            MAILBOX_IDENTITIES["bachelor_mail"]
        )
        self.assertEqual("READY", state)

    @patch("windows_gui.mail_summary._snapshot_edge_window")
    @patch("windows_gui.mail_summary.get_runtime_mailbox_context")
    def test_bachelor_new_tab_is_page_not_ready(
        self, get_context, snapshot
    ):
        get_context.return_value = SimpleNamespace(
            profile_directory="Profile 1", hwnd=82
        )
        snapshot.return_value = {"service_domain": None, "controls": []}
        _, state = _find_verified_snapshot(
            MAILBOX_IDENTITIES["bachelor_mail"]
        )
        self.assertEqual("PAGE_NOT_READY", state)

    @patch("windows_gui.mail_summary._snapshot_edge_window")
    @patch("windows_gui.mail_summary.get_runtime_mailbox_context")
    def test_identity_mismatch_stops_before_mail_parsing(
        self, get_context, snapshot
    ):
        get_context.return_value = SimpleNamespace(
            profile_directory="Profile 2", hwnd=89
        )
        snapshot.return_value = {
            "service_domain": "mail.qq.com",
            "controls": [{"control_type": "Hyperlink", "name": "mail row"}],
        }
        _, state = _find_verified_snapshot(MAILBOX_IDENTITIES["master_mail"])
        self.assertEqual("IDENTITY_MISMATCH", state)
        with (
            patch(
                "windows_gui.mail_summary._ensure_mailbox_page",
                return_value=(snapshot.return_value, state),
            ),
            patch("windows_gui.mail_summary._extract_today_emails") as extract,
        ):
            result = _summarize_mailbox(MAILBOX_IDENTITIES["master_mail"])
        self.assertEqual("IDENTITY_MISMATCH", result["status"])
        extract.assert_not_called()

    @patch("windows_gui.mail_summary._snapshot_edge_window")
    @patch("windows_gui.mail_summary.get_runtime_mailbox_context")
    def test_outlook_current_redirect_domain_is_ready(
        self, get_context, snapshot
    ):
        get_context.return_value = SimpleNamespace(
            profile_directory="Profile 2", hwnd=90
        )
        snapshot.return_value = {
            "service_domain": "outlook.cloud.microsoft", "controls": []
        }
        _, state = _find_verified_snapshot(MAILBOX_IDENTITIES["master_mail"])
        self.assertEqual("READY", state)

    @patch("windows_gui.mail_summary.get_runtime_mailbox_context", return_value=None)
    def test_unknown_window_stops(self, get_context):
        snapshot, state = _find_verified_snapshot(
            MAILBOX_IDENTITIES["qq_mail"]
        )
        self.assertIsNone(snapshot)
        self.assertEqual("UNKNOWN_WINDOW", state)

    def test_extracts_at_most_ten_today_rows_without_body_open(self):
        controls = []
        for index in range(12):
            controls.append({
                "control_type": "Hyperlink",
                "name": (
                    f"主题 {index} 发件人：Sender {index} "
                    "时间：2026年08月26日 10：05"
                ),
            })
        emails = _extract_today_emails(
            {"controls": controls}, today=date(2026, 8, 26)
        )
        self.assertEqual(10, len(emails))
        self.assertTrue(all(not item["read_state_changed"] for item in emails))
        self.assertTrue(all(item["summary_source"] == "LIST_METADATA" for item in emails))

    @patch("windows_gui.mail_summary._summarize_mailbox")
    def test_output_structure_and_order(self, summarize):
        def result(identity):
            return {
                "mailbox_id": identity.mailbox_id,
                "display_name": identity.display_name,
                "status": "READY",
                "message": "ok",
                "today_count": 0,
                "emails": [],
                "read_state_change": "NONE",
            }
        summarize.side_effect = result
        output = summarize_all_mailboxes_today()
        self.assertEqual(
            ["bachelor_mail", "master_mail", "qq_mail"],
            [item["mailbox_id"] for item in output["mailboxes"]],
        )
        self.assertIn("important_items", output)
        self.assertEqual(
            {
                "需要回复", "截止日期/DDL", "课程/学校通知",
                "账号/安全", "财务/账单", "其他重要事项",
            },
            set(output["important_items"]),
        )


if __name__ == "__main__":
    unittest.main()
