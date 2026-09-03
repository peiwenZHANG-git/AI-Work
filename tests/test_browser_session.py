"""Side-effect-free tests for the persistent browser session controller."""

from __future__ import annotations

from pathlib import Path
import socket
import tempfile
import threading
import time
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


class TargetClosedError(Exception):
    pass


class FlakyRuntime(FakeRuntime):
    pass


class FailingCloseRuntime(FlakyRuntime):
    def close(self):
        raise TargetClosedError("Target page, context or browser has been closed")


class CrashingInspectRuntime(FlakyRuntime):
    def inspect(self, max_chars):
        if len(FlakyRuntime.instances) == 1:
            raise TargetClosedError("Target page, context or browser has been closed")
        return {"status": "READY"}


def make_click_runtime():
    runtime = browser_session.PlaywrightRuntime.__new__(browser_session.PlaywrightRuntime)
    meta = {"tag": "button", "type": "submit", "role": ""}
    target = MagicMock()
    target.count.return_value = 1
    target.evaluate.side_effect = lambda expression, *args: (
        meta if "tagName" in expression else "Submit label"
    )
    located = MagicMock()
    located.count.return_value = 1
    located.evaluate.side_effect = lambda expression, *args: (
        meta if "tagName" in expression else "Submit label"
    )
    runtime._page = MagicMock()
    runtime._page.url = "https://example.com/form"
    runtime._page.get_by_text.return_value = target
    runtime._page.locator.return_value = located
    return runtime, target, located


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
        runtime, target, located = make_click_runtime()
        runtime._page.url = "https://example.com/form?token=private"

        result = runtime.click("Submit", True, False)
        self.assertEqual("CONFIRMATION_REQUIRED", result["status"])
        self.assertEqual(
            {"tag": "button", "type": "submit", "role": ""}, result["element"]
        )
        target.click.assert_not_called()
        located.click.assert_not_called()

        confirmed = runtime.click("Submit", True, True)
        self.assertEqual("CLICKED", confirmed["status"])
        self.assertEqual("https://example.com/form", confirmed["url"])
        located.click.assert_called_once_with()
        target.click.assert_not_called()
        self.assertNotIn("private", repr(confirmed))

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


class ConfirmationFlowTests(unittest.TestCase):
    def setUp(self):
        browser_session._CONFIRMATIONS.pending.clear()

    def tearDown(self):
        browser_session._CONFIRMATIONS.pending.clear()

    def test_confirm_without_staged_confirmation_is_rejected(self):
        runtime, target, located = make_click_runtime()
        result = runtime.click("Submit", True, True)
        self.assertEqual("CONFIRMATION_REQUIRED", result["status"])
        located.click.assert_not_called()
        target.click.assert_not_called()

    def test_staged_confirmation_executes_exactly_once(self):
        runtime, target, located = make_click_runtime()
        self.assertEqual(
            "CONFIRMATION_REQUIRED", runtime.click("Submit", True, False)["status"]
        )
        self.assertEqual("CLICKED", runtime.click("Submit", True, True)["status"])
        second = runtime.click("Submit", True, True)
        self.assertEqual("CONFIRMATION_REQUIRED", second["status"])
        self.assertEqual(1, located.click.call_count)

    def test_expired_staged_confirmation_is_rejected(self):
        runtime, target, located = make_click_runtime()
        runtime.click("Submit", True, False)
        task_id = runtime._pending_confirmation_task_id
        browser_session._CONFIRMATIONS.pending[task_id]["_expires_at_mono"] = 0
        result = runtime.click("Submit", True, True)
        self.assertEqual("CONFIRMATION_REQUIRED", result["status"])
        located.click.assert_not_called()

    def test_navigation_invalidates_staged_confirmation(self):
        runtime, target, located = make_click_runtime()
        runtime.click("Submit", True, False)
        runtime._navigation_epoch += 1
        with self.assertRaisesRegex(ValueError, "navigated"):
            runtime.click("Submit", True, True)
        located.click.assert_not_called()

    def test_changed_element_invalidates_staged_confirmation(self):
        runtime, target, located = make_click_runtime()
        runtime.click("Submit", True, False)
        changed = {"tag": "input", "type": "submit", "role": ""}
        located.evaluate.side_effect = lambda expression, *args: (
            changed if "tagName" in expression else "Submit label"
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            runtime.click("Submit", True, True)
        located.click.assert_not_called()

    def test_missing_element_invalidates_staged_confirmation(self):
        runtime, target, located = make_click_runtime()
        runtime.click("Submit", True, False)
        located.count.return_value = 0
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            runtime.click("Submit", True, True)
        located.click.assert_not_called()

    def test_confirmed_reference_cannot_execute_another_element(self):
        runtime, target, located = make_click_runtime()
        runtime.click("Submit", True, False)
        with self.assertRaisesRegex(ValueError, "does not match"):
            runtime.click("Delete", True, True)
        located.click.assert_not_called()
        result = runtime.click("Submit", True, True)
        self.assertEqual("CONFIRMATION_REQUIRED", result["status"])
        self.assertEqual(0, located.click.call_count)

    def test_previous_session_cannot_confirm_current_page(self):
        runtime, target, located = make_click_runtime()
        runtime.click("Submit", True, False)
        runtime.session_id = "replaced-session"
        with self.assertRaisesRegex(ValueError, "previous browser session"):
            runtime.click("Submit", True, True)
        located.click.assert_not_called()

    def test_verified_context_never_leaks_into_results(self):
        runtime, target, located = make_click_runtime()
        staged = runtime.click("Submit", True, False)
        confirmed = runtime.click("Submit", True, True)
        for result in (staged, confirmed):
            rendered = repr(result)
            self.assertNotIn("fingerprint", rendered)
            self.assertNotIn("token", rendered.casefold())
            self.assertNotIn("request", rendered)


class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        FlakyRuntime.instances.clear()
        browser_session._CONFIRMATIONS.pending.clear()

    def tearDown(self):
        FlakyRuntime.instances.clear()
        browser_session._CONFIRMATIONS.pending.clear()

    def _controller(self, factory):
        events = []
        controller = browser_session.BrowserSessionController(
            factory, event_recorder=lambda *args, **kwargs: events.append(args)
        )
        return controller, events

    @staticmethod
    def _dead_thread():
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        return dead

    def test_dead_worker_is_recovered_once_and_serves_again(self):
        with tempfile.TemporaryDirectory() as local_app_data, patch.dict(
            "os.environ", {"LOCALAPPDATA": local_app_data}
        ):
            controller, events = self._controller(FlakyRuntime)
            controller.start()
            controller._thread = self._dead_thread()
            self.assertEqual("READY", controller.call("inspect", 10)["status"])
            self.assertEqual(2, len(FlakyRuntime.instances))
            self.assertEqual(
                ("browser_session", "success", "worker_recovered"), events[-1]
            )
            controller.call("stop")

    def test_session_fatal_error_recovers_on_next_call(self):
        with tempfile.TemporaryDirectory() as local_app_data, patch.dict(
            "os.environ", {"LOCALAPPDATA": local_app_data}
        ):
            controller, events = self._controller(CrashingInspectRuntime)
            controller.start()
            with self.assertRaises(TargetClosedError):
                controller.call("inspect", 10)
            self.assertEqual("READY", controller.call("inspect", 10)["status"])
            self.assertEqual(2, len(CrashingInspectRuntime.instances))

    def test_recovery_failures_are_bounded(self):
        def factory(*, headless, profile_directory):
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as local_app_data, patch.dict(
            "os.environ", {"LOCALAPPDATA": local_app_data}
        ):
            controller, events = self._controller(factory)
            with self.assertRaisesRegex(RuntimeError, "boom"):
                controller.start()
            for _ in range(3):
                with self.assertRaisesRegex(RuntimeError, "recovery failed"):
                    controller.call("inspect", 10)
            with self.assertRaisesRegex(RuntimeError, "recovery limit"):
                controller.call("inspect", 10)
            recovery_failures = [
                event for event in events if event[2] == "worker_recovery_failed"
            ]
            self.assertEqual(3, len(recovery_failures))

    def test_concurrent_calls_spawn_a_single_recovery_worker(self):
        creation_lock = threading.Lock()
        creations = []

        def factory(*, headless, profile_directory):
            with creation_lock:
                creations.append(len(creations))
            time.sleep(0.05)
            return FlakyRuntime(headless=headless, profile_directory=profile_directory)

        with tempfile.TemporaryDirectory() as local_app_data, patch.dict(
            "os.environ", {"LOCALAPPDATA": local_app_data}
        ):
            controller, events = self._controller(factory)
            controller.start()
            controller._thread = self._dead_thread()
            results = []
            errors = []
            results_lock = threading.Lock()

            def call_inspect():
                try:
                    status = controller.call("inspect", 10)["status"]
                except BaseException as error:
                    with results_lock:
                        errors.append(error)
                else:
                    with results_lock:
                        results.append(status)

            threads = [threading.Thread(target=call_inspect) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual([], errors)
            self.assertEqual(["READY"] * 8, results)
            self.assertEqual([0, 1], creations)

    def test_stop_prevents_automatic_revival(self):
        with tempfile.TemporaryDirectory() as local_app_data, patch.dict(
            "os.environ", {"LOCALAPPDATA": local_app_data}
        ):
            controller, events = self._controller(FlakyRuntime)
            controller.start()
            self.assertEqual("STOPPED", controller.call("stop")["status"])
            with self.assertRaisesRegex(RuntimeError, "not running"):
                controller.call("inspect", 10)
            self.assertEqual(1, len(FlakyRuntime.instances))

    def test_failed_stop_does_not_revive_the_session(self):
        with tempfile.TemporaryDirectory() as local_app_data, patch.dict(
            "os.environ", {"LOCALAPPDATA": local_app_data}
        ):
            controller, events = self._controller(FailingCloseRuntime)
            controller.start()
            with self.assertRaises(TargetClosedError):
                controller.call("stop")
            with self.assertRaisesRegex(RuntimeError, "not running"):
                controller.call("inspect", 10)
            self.assertEqual(1, len(FailingCloseRuntime.instances))

    def test_worker_restart_invalidates_pending_confirmations(self):
        with tempfile.TemporaryDirectory() as local_app_data, patch.dict(
            "os.environ", {"LOCALAPPDATA": local_app_data}
        ):
            controller, events = self._controller(FlakyRuntime)
            controller.start()
            task_id = browser_session._CONFIRMATIONS.stage(
                "browser",
                "confirm_click",
                {"request": {"text": "Submit", "exact": True}},
            )
            controller._thread = self._dead_thread()
            controller.call("inspect", 10)
            view = browser_session._CONFIRMATIONS.lookup(task_id)
            self.assertEqual("CANCELLED", view.state)
            self.assertEqual(0, browser_session._CONFIRMATIONS.pending_count())


class RequestGuardTests(unittest.TestCase):
    def test_request_allowed_fails_closed_on_private_resolution(self):
        private = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
        ]
        public = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443)),
        ]
        with patch.object(socket, "getaddrinfo", return_value=private):
            self.assertFalse(
                browser_session._request_allowed("https://example.com/page")
            )
        with patch.object(socket, "getaddrinfo", return_value=public):
            self.assertTrue(
                browser_session._request_allowed("https://example.com/page")
            )


if __name__ == "__main__":
    unittest.main()
