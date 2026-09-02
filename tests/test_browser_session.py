"""Side-effect-free tests for the persistent browser session controller."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from windows_gui import browser_session


class FakeRuntime:
    instances = []

    def __init__(self, *, headless, profile_directory):
        self.headless = headless
        self.profile_directory = profile_directory
        self.calls = []
        self.closed = False
        self.__class__.instances.append(self)

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return {"status": "NAVIGATED", "url": url}

    def inspect(self, max_chars):
        self.calls.append(("inspect", max_chars))
        return {"status": "READY", "text": "page"}

    def click(self, text, exact, confirm):
        self.calls.append(("click", text, exact, confirm))
        return {"status": "CLICKED"}

    def download(self, text, destination, filename, exact):
        self.calls.append(("download", text, destination, filename, exact))
        return {"status": "DOWNLOADED"}

    def close(self):
        self.closed = True
        return {"status": "STOPPED"}


class BrowserSessionControllerTests(unittest.TestCase):
    def setUp(self):
        FakeRuntime.instances.clear()

    def test_runtime_stays_on_one_worker_and_dispatches_commands(self):
        controller = browser_session.BrowserSessionController(FakeRuntime)
        with tempfile.TemporaryDirectory() as local_app_data, patch.dict(
            "os.environ", {"LOCALAPPDATA": local_app_data}
        ):
            self.assertEqual("STARTED", controller.start()["status"])
            self.assertEqual("ALREADY_RUNNING", controller.start()["status"])
            self.assertEqual("NAVIGATED", controller.call("navigate", "https://example.com")["status"])
            self.assertEqual("READY", controller.call("inspect", 100)["status"])
            self.assertEqual("STOPPED", controller.call("stop")["status"])
        runtime = FakeRuntime.instances[0]
        self.assertEqual(Path(local_app_data) / "AI-Work" / "browser-agent-profile", runtime.profile_directory)
        self.assertTrue(runtime.closed)

    def test_call_before_start_is_rejected(self):
        controller = browser_session.BrowserSessionController(FakeRuntime)
        with self.assertRaisesRegex(RuntimeError, "not running"):
            controller.call("inspect", 100)

    def test_calls_fail_after_stop(self):
        controller = browser_session.BrowserSessionController(FakeRuntime)
        with tempfile.TemporaryDirectory() as local_app_data, patch.dict(
            "os.environ", {"LOCALAPPDATA": local_app_data}
        ):
            controller.start()
            self.assertEqual("STOPPED", controller.call("stop")["status"])
            with self.assertRaisesRegex(RuntimeError, "not running"):
                controller.call("inspect", 100)


class BrowserSessionToolTests(unittest.TestCase):
    def test_tools_delegate_without_launching_browser(self):
        fake = MagicMock()
        fake.start.return_value = {"status": "STARTED"}
        fake.call.side_effect = lambda action, *args: {"action": action, "args": args}
        with patch.object(browser_session, "_SESSION", fake):
            self.assertEqual("STARTED", browser_session.start_browser_session()["status"])
            self.assertEqual("navigate", browser_session.navigate_browser("https://example.com")["action"])
            self.assertEqual("inspect", browser_session.inspect_browser()["action"])
            self.assertEqual("click", browser_session.click_browser_element("Next")["action"])
            self.assertEqual("download", browser_session.download_browser_element("PDF", "D:/Downloads")["action"])
            self.assertEqual("stop", browser_session.stop_browser_session()["action"])


class PlaywrightRuntimeSafetyTests(unittest.TestCase):
    def test_inspect_redacts_password_values_and_url_queries(self):
        runtime = browser_session.PlaywrightRuntime.__new__(browser_session.PlaywrightRuntime)
        links = MagicMock()
        links.evaluate_all.return_value = [
            {"text": "Dashboard", "href": "https://example.com/app?token=private#settings"},
        ]
        body = MagicMock()
        body.inner_text.return_value = "safe page text"
        controls = MagicMock()
        controls.evaluate_all.return_value = [
            {"tag": "input", "type": "text", "value": "token-secret", "label": "Username"},
            {"tag": "input", "type": "password", "text": "", "value": "password-secret"},
        ]
        runtime._page = MagicMock()
        runtime._page.url = "https://example.com/app?session=private#account"
        runtime._page.locator.side_effect = lambda selector: (
            body if selector == "body" else links if selector == "a" else controls
        )

        result = runtime.inspect(100)
        rendered = repr(result)
        self.assertEqual("https://example.com/app", result["url"])
        self.assertEqual("https://example.com/app", result["links"][0]["url"])
        self.assertNotIn("private", rendered)
        self.assertNotIn("password-secret", rendered)
        self.assertNotIn("token-secret", rendered)
        self.assertIn("Username", rendered)
        self.assertNotIn(".value", browser_session._INSPECT_CONTROLS_EXPRESSION)

    def test_route_guard_rejects_private_urls_before_browser_continues(self):
        runtime = browser_session.PlaywrightRuntime.__new__(browser_session.PlaywrightRuntime)
        safe_route = MagicMock()
        private_route = MagicMock()
        safe_route.request.url = "https://example.com/page"
        private_route.request.url = "https://10.0.0.8/page"
        def validator(url, **kwargs):
            if url == safe_route.request.url:
                return url
            raise ValueError("private URL")

        with patch.object(browser_session, "_validate_public_host", side_effect=validator):
            runtime._route_guard(safe_route)
            runtime._route_guard(private_route)
        safe_route.continue_.assert_called_once_with()
        private_route.continue_.assert_not_called()
        private_route.abort.assert_called_once_with()

    def test_button_click_requires_confirmation_even_for_nested_text(self):
        runtime = browser_session.PlaywrightRuntime.__new__(browser_session.PlaywrightRuntime)
        target = MagicMock()
        target.count.return_value = 1
        target.evaluate.return_value = {"tag": "button", "type": "submit", "role": ""}
        runtime._page = MagicMock()
        runtime._page.url = "https://example.com/form?token=private"
        runtime._page.get_by_text.return_value = target

        result = runtime.click("Submit", True, False)
        self.assertEqual("CONFIRMATION_REQUIRED", result["status"])
        target.click.assert_not_called()

        runtime.click("Submit", True, True)
        target.click.assert_called_once_with()

    def test_non_unique_element_click_is_rejected(self):
        runtime = browser_session.PlaywrightRuntime.__new__(browser_session.PlaywrightRuntime)
        target = MagicMock()
        target.count.return_value = 2
        runtime._page = MagicMock()
        runtime._page.url = "https://example.com/files/report"
        runtime._page.get_by_text.return_value = target
        with self.assertRaisesRegex(ValueError, "matched 2 elements"):
            runtime.click("Delete", True, True)
        target.click.assert_not_called()

    def test_browser_download_limits_size_avoids_overwrite_and_cleans_temporary(self):
        runtime = browser_session.PlaywrightRuntime.__new__(browser_session.PlaywrightRuntime)
        target = MagicMock()
        target.count.return_value = 1
        event = MagicMock()
        download = MagicMock()
        runtime._page = MagicMock()
        runtime._page.url = "https://example.com/files/report"
        runtime._page.get_by_text.return_value = target
        runtime._page.expect_download.return_value.__enter__.return_value = event
        event.value = download
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "browser-source.bin")
            source.write_bytes(b"1234")
            download.path.return_value = source
            Path(directory, "report.bin").write_bytes(b"existing")
            with patch.object(
                browser_session, "_MAX_BROWSER_DOWNLOAD_BYTES", 4
            ), self.assertRaises(FileExistsError):
                runtime.download("Download", directory, "report.bin", True)
            download.delete.assert_not_called()
            target.click.assert_called_once_with()
            self.assertEqual([], [path.name for path in Path(directory).glob("*.part")])
            self.assertEqual(b"existing", Path(directory, "report.bin").read_bytes())

            Path(directory, "report.bin").unlink()
            source.write_bytes(b"12345")
            with patch.object(
                browser_session, "_MAX_BROWSER_DOWNLOAD_BYTES", 4
            ):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    runtime.download("Download", directory, "oversize.bin", True)
            download.delete.assert_not_called()
            self.assertEqual([], [path.name for path in Path(directory).glob("*.part")])

            source.write_bytes(b"1234")
            with patch.object(
                browser_session, "_MAX_BROWSER_DOWNLOAD_BYTES", 4
            ):
                result = runtime.download("Download", directory, "report.bin", True)
            self.assertEqual(4, result["size_bytes"])
            download.delete.assert_called_once_with()
            self.assertEqual([], [path.name for path in Path(directory).glob("*.part")])


if __name__ == "__main__":
    unittest.main()
