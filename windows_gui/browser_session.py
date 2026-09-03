"""Persistent Playwright browser session owned by one dedicated worker thread."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import hashlib
import secrets
import threading
import tempfile
from typing import Any, Callable

from .browser_download import (
    DEFAULT_MAX_BYTES, _safe_filename, _validate_public_host, redact_web_url,
)
from .health_events import record_health_event
from .mailboxes import _find_edge_executable
from .server import mcp
from .task_center import TaskCenter, TaskCenterError


_DEFAULT_TIMEOUT_SECONDS = 15.0
_MAX_INSPECT_CHARS = 40_000
_MAX_BROWSER_DOWNLOAD_BYTES = DEFAULT_MAX_BYTES
_MAX_CONSECUTIVE_RECOVERIES = 3
_CONFIRMATION_TTL_SECONDS = 120
_CONFIRM_MARKER_ATTRIBUTE = 'data-ai-work-confirm'
_INSPECT_CONTROLS_EXPRESSION = (
    "els => els.slice(0, 100).map(e => ({tag: e.tagName.toLowerCase(), "
    "type: e.type || '', text: (e.innerText || "
    "e.getAttribute('aria-label') || e.name || '').trim().slice(0, 300)}))"
)
_ELEMENT_META_EXPRESSION = (
    "e => { const c = e.closest('button,input,select,textarea,[role=button],a') || e; "
    "return ({tag: c.tagName.toLowerCase(), type: (c.type || '').toLowerCase(), "
    "role: (c.getAttribute('role') || '').toLowerCase()}); }"
)
_ELEMENT_TEXT_EXPRESSION = (
    "e => { const c = e.closest('button,input,select,textarea,[role=button],a') || e; "
    "return (((c.innerText || c.getAttribute('aria-label') || c.name || '') + '')"
    ".trim().slice(0, 300)); }"
)
_STAGE_MARKER_EXPRESSION = (
    "(e, token) => { const c = e.closest('button,input,select,textarea,[role=button],a') || e; "
    "c.setAttribute('data-ai-work-confirm', token); "
    "return (((c.innerText || c.getAttribute('aria-label') || c.name || '') + '')"
    ".trim().slice(0, 300)); }"
)
_REMOVE_MARKER_EXPRESSION = (
    "e => { e.removeAttribute('data-ai-work-confirm'); }"
)

_CONFIRMATIONS = TaskCenter(
    domains=('browser',),
    action_types=('confirm_click',),
    ttl_seconds=_CONFIRMATION_TTL_SECONDS,
    max_tasks=4,
)


def _request_allowed(url: str) -> bool:
    """Fail closed for any URL whose resolved hosts are not provably public."""
    try:
        _validate_public_host(url, allow_http=True)
    except (ValueError, OSError):
        return False
    return True


def _is_session_fatal(error: BaseException) -> bool:
    """Return True only when the page, context, or browser itself is gone."""
    if type(error).__name__ == 'TargetClosedError':
        return True
    text = str(error).casefold()
    return (
        'target page, context or browser has been closed' in text
        or 'browser has been closed' in text
        or 'context has been closed' in text
    )


def _record_confirmation_outcome(task_id: str, *, success: bool) -> None:
    try:
        _CONFIRMATIONS.complete(task_id, success=success)
    except TaskCenterError:
        pass


def _profile_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is not configured")
    return Path(local) / "AI-Work" / "browser-agent-profile"


class PlaywrightRuntime:
    """All methods are called only from the controller's worker thread."""

    session_id = ''
    _navigation_epoch = 0
    _pending_confirmation_task_id = ''

    def __init__(self, *, headless: bool, profile_directory: Path) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "Playwright is not installed; run: python -m pip install playwright"
            ) from error
        profile_directory.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            str(profile_directory), executable_path=_find_edge_executable(),
            headless=headless, accept_downloads=True,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(10_000)
        self.session_id = secrets.token_hex(16)
        self._navigation_epoch = 0
        self._pending_confirmation_task_id = ''
        self._context.route("**/*", self._route_guard)
        self._page.on("framenavigated", self._on_frame_navigated)

    def _on_frame_navigated(self, frame: Any) -> None:
        if frame is getattr(self._page, "main_frame", None):
            self._navigation_epoch += 1

    def _route_guard(self, route: Any) -> None:
        if _request_allowed(route.request.url):
            route.continue_()
        else:
            route.abort()

    def close(self) -> dict[str, str]:
        try:
            self._context.unroute("**/*", self._route_guard)
        finally:
            try:
                self._page.remove_listener("framenavigated", self._on_frame_navigated)
            except Exception:
                pass
            self._context.close()
            self._playwright.stop()
        return {"status": "STOPPED"}

    def navigate(self, url: str) -> dict[str, Any]:
        validated = _validate_public_host(url, allow_http=True)
        response = self._page.goto(validated, wait_until="domcontentloaded", timeout=30_000)
        return {
            "status": "NAVIGATED", "url": redact_web_url(self._page.url),
            "title": self._page.title()[:500],
            "http_status": response.status if response is not None else None,
        }

    def inspect(self, max_chars: int) -> dict[str, Any]:
        limit = max(1, min(int(max_chars), _MAX_INSPECT_CHARS))
        text = self._page.locator("body").inner_text(timeout=10_000)
        links = self._page.locator("a").evaluate_all(
            "els => els.slice(0, 100).map(e => ({text: (e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 300), href: e.href || ''}))"
        )
        safe_links = [
            {"text": item.get("text", ""), "url": redact_web_url(item["href"])}
            for item in links
            if item.get("href", "").startswith(("http://", "https://"))
        ]
        controls = self._page.locator("button, input, select, textarea").evaluate_all(
            _INSPECT_CONTROLS_EXPRESSION,
        )
        safe_controls = []
        for control in controls:
            control_type = control.get("type", "").lower()
            if control_type == "password":
                control_text = ""
            else:
                control_text = (
                    control.get("text") or control.get("label")
                    or control.get("name") or ""
                )
            safe_controls.append({
                "tag": control.get("tag", ""), "type": control_type,
                "text": control_text.strip()[:300],
            })
        return {
            "status": "READY", "url": redact_web_url(self._page.url),
            "title": self._page.title()[:500], "text": text[:limit],
            "text_truncated": len(text) > limit, "links": safe_links,
            "controls": safe_controls,
        }

    def _unique_target(self, text: str, exact: bool) -> Any:
        if not text or len(text) > 500 or any(char in text for char in "\r\n\x00"):
            raise ValueError("element text must contain 1 to 500 characters on one line")
        target = self._page.get_by_text(text, exact=exact)
        count = target.count()
        if count != 1:
            raise ValueError(f"element text matched {count} elements; exactly one is required")
        return target

    def click(self, text: str, exact: bool, confirm: bool) -> dict[str, Any]:
        target = self._unique_target(text, exact)
        element = target.evaluate(_ELEMENT_META_EXPRESSION)
        potentially_mutating = (
            element["tag"] in {"button", "input", "select", "textarea"}
            or element["role"] == "button"
        )
        if not potentially_mutating:
            target.click()
            self._page.wait_for_timeout(250)
            return {"status": "CLICKED", "url": redact_web_url(self._page.url), "element": element}
        if not confirm:
            return self._stage_click_confirmation(target, element, text, exact)
        return self._execute_confirmed_click(text, exact)

    def _stage_click_confirmation(
        self, target: Any, element: dict[str, Any], text: str, exact: bool,
    ) -> dict[str, Any]:
        self._discard_pending_confirmation()
        marker_token = secrets.token_urlsafe(16)
        element_text = target.evaluate(_STAGE_MARKER_EXPRESSION, marker_token)
        task_id = _CONFIRMATIONS.stage('browser', 'confirm_click', {
            'request': {'text': text, 'exact': bool(exact)},
            'fingerprint': {**element, 'text': element_text},
            'epoch': self._navigation_epoch,
            'session_id': self.session_id,
            'token': marker_token,
        }, ttl_seconds=_CONFIRMATION_TTL_SECONDS)
        self._pending_confirmation_task_id = task_id
        return {
            "status": "CONFIRMATION_REQUIRED", "element": element,
            "message": "button or form-control clicks require explicit confirmation",
        }

    def _execute_confirmed_click(self, text: str, exact: bool) -> dict[str, Any]:
        task_id = self._pending_confirmation_task_id
        if not task_id:
            return {
                "status": "CONFIRMATION_REQUIRED", "element": {},
                "message": (
                    "no staged confirmation; repeat the request without "
                    "confirm to stage it"
                ),
            }
        try:
            context = _CONFIRMATIONS.consume(task_id)
        except TaskCenterError:
            self._pending_confirmation_task_id = ''
            return {
                "status": "CONFIRMATION_REQUIRED", "element": {},
                "message": (
                    "staged confirmation expired or was already used; "
                    "stage it again without confirm"
                ),
            }
        self._pending_confirmation_task_id = ''
        try:
            result = self._verified_confirmed_click(context, text, exact)
        except BaseException:
            _record_confirmation_outcome(task_id, success=False)
            raise
        _record_confirmation_outcome(task_id, success=True)
        return result

    def _verified_confirmed_click(
        self, context: dict[str, Any], text: str, exact: bool,
    ) -> dict[str, Any]:
        request = context.get('request') or {}
        if request.get('text') != text or bool(request.get('exact')) != bool(exact):
            raise ValueError('staged confirmation does not match the requested element')
        if context.get('session_id') != self.session_id:
            raise ValueError('staged confirmation belongs to a previous browser session')
        if context.get('epoch') != self._navigation_epoch:
            raise ValueError('page has navigated since the confirmation was staged')
        marker_token = str(context.get('token') or '')
        if not marker_token:
            raise ValueError('staged confirmation is missing its target reference')
        located = self._page.locator(f'[{_CONFIRM_MARKER_ATTRIBUTE}="{marker_token}"]')
        if located.count() != 1:
            raise ValueError('confirmed element no longer exists on the page')
        element = located.evaluate(_ELEMENT_META_EXPRESSION)
        element_text = located.evaluate(_ELEMENT_TEXT_EXPRESSION)
        staged = context.get('fingerprint') or {}
        if (
            element.get('tag'), element.get('type'), element.get('role'), element_text
        ) != (
            staged.get('tag'), staged.get('type'), staged.get('role'), staged.get('text'),
        ):
            raise ValueError('confirmed element changed after staging')
        try:
            located.evaluate(_REMOVE_MARKER_EXPRESSION)
        except Exception:
            pass
        located.click()
        self._page.wait_for_timeout(250)
        return {"status": "CLICKED", "url": redact_web_url(self._page.url), "element": element}

    def _discard_pending_confirmation(self) -> None:
        task_id = self._pending_confirmation_task_id
        if task_id:
            try:
                _CONFIRMATIONS.cancel(task_id)
            except TaskCenterError:
                pass
            self._pending_confirmation_task_id = ''

    def download(self, text: str, destination_directory: str, filename: str, exact: bool) -> dict[str, Any]:
        destination = Path(destination_directory).expanduser().resolve()
        if not destination.is_dir():
            raise ValueError("destination directory must already exist")
        target = self._unique_target(text, exact)
        with self._page.expect_download(timeout=30_000) as event:
            target.click()
        download = event.value
        safe_name = _safe_filename(filename or download.suggested_filename)
        output = destination / safe_name
        if output.exists():
            raise FileExistsError(f"destination already exists: {output.name}")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".browser-download-", suffix=".part", dir=destination,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            source = Path(download.path())
            digest = hashlib.sha256()
            size = 0
            with source.open("rb") as source_stream:
                with temporary.open("wb") as output_stream:
                    for chunk in iter(lambda: source_stream.read(64 * 1024), b""):
                        size += len(chunk)
                        if size > _MAX_BROWSER_DOWNLOAD_BYTES:
                            raise ValueError("download exceeds the configured size limit")
                        digest.update(chunk)
                        output_stream.write(chunk)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            download.delete()
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "status": "DOWNLOADED", "path": str(output),
            "filename": safe_name, "size_bytes": size,
            "sha256": digest.hexdigest(),
            "url": redact_web_url(self._page.url),
        }


class BrowserSessionController:
    _MAX_CONSECUTIVE_RECOVERIES = 3

    def __init__(
        self,
        runtime_factory: Callable[..., Any] = PlaywrightRuntime,
        event_recorder: Callable[..., Any] = record_health_event,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._event_recorder = event_recorder
        self._lock = threading.Lock()
        self._commands: queue.Queue[Any] | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopped = False
        self._headless = False
        self._recoveries_since_healthy = 0

    def _record_event(self, outcome: str, code: str) -> None:
        try:
            self._event_recorder('browser_session', outcome, code)
        except Exception:
            pass

    def _spawn_worker_locked(
        self, headless: bool,
    ) -> tuple[queue.Queue[Any], threading.Thread]:
        commands: queue.Queue[Any] = queue.Queue()
        ready: queue.Queue[Any] = queue.Queue(maxsize=1)
        thread = threading.Thread(
            target=self._worker, args=(commands, ready, headless),
            name="browser-agent-playwright", daemon=True,
        )
        self._commands = commands
        self._thread = thread
        self._headless = headless
        thread.start()
        return ready, thread

    def start(self, *, headless: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"status": "ALREADY_RUNNING", "headless": headless}
            self._started = True
            self._stopped = False
            self._recoveries_since_healthy = 0
            _CONFIRMATIONS.cancel_all_staged()
            ready, thread = self._spawn_worker_locked(headless)
        initialized = ready.get(timeout=_DEFAULT_TIMEOUT_SECONDS)
        if isinstance(initialized, BaseException):
            with self._lock:
                self._commands = None
                self._thread = None
            raise initialized
        return {"status": "STARTED", "headless": headless, "profile": "dedicated-agent-profile"}

    def _recover_locked(self) -> None:
        if self._recoveries_since_healthy >= self._MAX_CONSECUTIVE_RECOVERIES:
            raise RuntimeError(
                "browser session recovery limit reached; restart the session"
            )
        _CONFIRMATIONS.cancel_all_staged()
        self._recoveries_since_healthy += 1
        ready, thread = self._spawn_worker_locked(self._headless)
        initialized = ready.get(timeout=_DEFAULT_TIMEOUT_SECONDS)
        if isinstance(initialized, BaseException):
            self._commands = None
            self._thread = None
            self._record_event('error', 'worker_recovery_failed')
            raise RuntimeError("browser session worker recovery failed") from initialized
        self._record_event('success', 'worker_recovered')

    def _shutdown_worker_locked(self) -> None:
        commands = self._commands
        self._commands = None
        self._thread = None
        if commands is not None:
            try:
                commands.put_nowait(("shutdown", (), queue.Queue(maxsize=1)))
            except Exception:
                pass

    def _worker(self, commands: queue.Queue[Any], ready: queue.Queue[Any], headless: bool) -> None:
        try:
            runtime = self._runtime_factory(headless=headless, profile_directory=_profile_root())
        except BaseException as error:
            ready.put(error)
            return
        ready.put(True)
        while True:
            action, arguments, result = commands.get()
            try:
                if action == "stop":
                    result.put(runtime.close())
                    return
                if action == "shutdown":
                    return
                result.put(getattr(runtime, action)(*arguments))
            except BaseException as error:
                result.put(error)

    def call(self, action: str, *arguments: Any, timeout: float = 45.0) -> Any:
        with self._lock:
            if action == "stop":
                self._stopped = True
            if (
                self._commands is None or self._thread is None
                or not self._thread.is_alive()
            ):
                if self._stopped or not self._started:
                    raise RuntimeError("browser session is not running")
                self._recover_locked()
            commands = self._commands
            thread = self._thread
            result: queue.Queue[Any] = queue.Queue(maxsize=1)
            commands.put((action, arguments, result))
        value = result.get(timeout=timeout)
        if isinstance(value, BaseException):
            if action == "stop" or _is_session_fatal(value):
                with self._lock:
                    self._shutdown_worker_locked()
            raise value
        with self._lock:
            self._recoveries_since_healthy = 0
            if action == "stop":
                thread.join(timeout=5.0)
                self._shutdown_worker_locked()
        return value


_SESSION = BrowserSessionController()


@mcp.tool()
def start_browser_session(headless: bool = False) -> dict[str, Any]:
    """Start the dedicated persistent Edge session; login data stays in its private profile."""
    return _SESSION.start(headless=headless)


@mcp.tool()
def navigate_browser(url: str) -> dict[str, Any]:
    """Navigate the running persistent browser session to a public HTTP(S) URL."""
    return _SESSION.call("navigate", url)


@mcp.tool()
def inspect_browser(max_chars: int = 12_000) -> dict[str, Any]:
    """Read bounded visible page text, links, and controls without returning cookies or tokens."""
    return _SESSION.call("inspect", max_chars)


@mcp.tool()
def click_browser_element(text: str, exact: bool = True, confirm: bool = False) -> dict[str, Any]:
    """Click one uniquely matched visible element; buttons and form controls require confirm=true."""
    return _SESSION.call("click", text, exact, confirm)


@mcp.tool()
def download_browser_element(
    text: str, destination_directory: str, filename: str = "", exact: bool = True,
) -> dict[str, Any]:
    """Click one uniquely matched element and save its browser-authenticated download without overwrite."""
    return _SESSION.call("download", text, destination_directory, filename, exact)


@mcp.tool()
def stop_browser_session() -> dict[str, Any]:
    """Close the dedicated persistent browser session while preserving its private login profile."""
    return _SESSION.call("stop")


__all__ = [
    "BrowserSessionController", "PlaywrightRuntime", "click_browser_element",
    "download_browser_element", "inspect_browser", "navigate_browser",
    "start_browser_session", "stop_browser_session",
]
