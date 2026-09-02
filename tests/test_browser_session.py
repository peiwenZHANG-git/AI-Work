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


if __name__ == "__main__":
    unittest.main()
