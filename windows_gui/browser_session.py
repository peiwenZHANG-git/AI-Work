"""Persistent Playwright browser session owned by one dedicated worker thread."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import hashlib
import threading
import tempfile
from typing import Any, Callable

from .browser_download import (
    DEFAULT_MAX_BYTES, _safe_filename, _validate_public_host, redact_web_url,
)
from .mailboxes import _find_edge_executable
from .server import mcp


_DEFAULT_TIMEOUT_SECONDS = 15.0
_MAX_INSPECT_CHARS = 40_000
_MAX_BROWSER_DOWNLOAD_BYTES = DEFAULT_MAX_BYTES
_INSPECT_CONTROLS_EXPRESSION = (
    "els => els.slice(0, 100).map(e => ({tag: e.tagName.toLowerCase(), "
    "type: e.type || '', text: (e.innerText || "
    "e.getAttribute('aria-label') || e.name || '').trim().slice(0, 300)}))"
)


def _profile_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is not configured")
    return Path(local) / "AI-Work" / "browser-agent-profile"


class PlaywrightRuntime:
    """All methods are called only from the controller's worker thread."""

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
        self._context.route("**/*", self._route_guard)

    def _route_guard(self, route: Any) -> None:
        try:
            _validate_public_host(route.request.url, allow_http=True)
            route.continue_()
        except (ValueError, OSError):
            route.abort()

    def close(self) -> dict[str, str]:
        try:
            self._context.unroute("**/*", self._route_guard)
        finally:
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
        element = target.evaluate(
            "e => { const c = e.closest('button,input,select,textarea,[role=button],a') || e; return ({tag: c.tagName.toLowerCase(), type: (c.type || '').toLowerCase(), role: (c.getAttribute('role') || '').toLowerCase()}); }"
        )
        potentially_mutating = element["tag"] in {"button", "input", "select", "textarea"} or element["role"] == "button"
        if potentially_mutating and not confirm:
            return {
                "status": "CONFIRMATION_REQUIRED", "element": element,
                "message": "button or form-control clicks require explicit confirmation",
            }
        target.click()
        self._page.wait_for_timeout(250)
        return {"status": "CLICKED", "url": redact_web_url(self._page.url), "element": element}

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
    def __init__(self, runtime_factory: Callable[..., Any] = PlaywrightRuntime) -> None:
        self._runtime_factory = runtime_factory
        self._lock = threading.Lock()
        self._commands: queue.Queue[Any] | None = None
        self._thread: threading.Thread | None = None

    def start(self, *, headless: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"status": "ALREADY_RUNNING", "headless": headless}
            commands: queue.Queue[Any] = queue.Queue()
            ready: queue.Queue[Any] = queue.Queue(maxsize=1)
            thread = threading.Thread(
                target=self._worker, args=(commands, ready, headless),
                name="browser-agent-playwright", daemon=True,
            )
            self._commands = commands
            self._thread = thread
            thread.start()
        initialized = ready.get(timeout=_DEFAULT_TIMEOUT_SECONDS)
        if isinstance(initialized, BaseException):
            with self._lock:
                self._commands = None
                self._thread = None
            raise initialized
        return {"status": "STARTED", "headless": headless, "profile": "dedicated-agent-profile"}

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
                result.put(getattr(runtime, action)(*arguments))
            except BaseException as error:
                result.put(error)

    def call(self, action: str, *arguments: Any, timeout: float = 45.0) -> Any:
        with self._lock:
            commands = self._commands
            thread = self._thread
            if commands is None or thread is None or not thread.is_alive():
                raise RuntimeError("browser session is not running")
            result: queue.Queue[Any] = queue.Queue(maxsize=1)
            commands.put((action, arguments, result))
        value = result.get(timeout=timeout)
        if isinstance(value, BaseException):
            raise value
        if action == "stop":
            thread.join(timeout=5.0)
            with self._lock:
                self._commands = None
                self._thread = None
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
