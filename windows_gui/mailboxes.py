"""Fixed mailbox identities and safe Microsoft Edge launch helpers."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Final

import win32gui
import win32process
from pywinauto import Desktop

from .keyboard import hotkey, press_key, type_text
from .server import mcp
from .uia import _run_bounded
from .windows import _focus_window_handle


@dataclass(frozen=True)
class MailboxIdentity:
    """Non-secret metadata used to select the correct browser identity."""

    mailbox_id: str
    display_name: str
    browser: str
    profile_directory: str
    service: str
    permissions: tuple[str, ...]
    send_requires_confirmation: bool
    service_domain: str
    service_domain_aliases: tuple[str, ...] = ()
    stable_url: str | None = None


_MAILBOXES = {
    "bachelor_mail": MailboxIdentity(
        mailbox_id="bachelor_mail",
        display_name="本科邮箱",
        browser="Microsoft Edge",
        profile_directory="Profile 1",
        service="网易企业邮箱",
        permissions=("READ", "DRAFT", "SEND"),
        send_requires_confirmation=True,
        service_domain="mailh.qiye.163.com",
        stable_url="https://mailh.qiye.163.com/",
    ),
    "master_mail": MailboxIdentity(
        mailbox_id="master_mail",
        display_name="硕士邮箱",
        browser="Microsoft Edge",
        profile_directory="Profile 2",
        service="Outlook Web",
        stable_url="https://outlook.office.com/mail/",
        permissions=("READ", "DRAFT", "SEND"),
        send_requires_confirmation=True,
        service_domain="outlook.office.com",
        service_domain_aliases=("outlook.cloud.microsoft",),
    ),
    "qq_mail": MailboxIdentity(
        mailbox_id="qq_mail",
        display_name="QQ邮箱",
        browser="Microsoft Edge",
        profile_directory="Profile 3",
        service="QQ Mail",
        stable_url="https://mail.qq.com/",
        permissions=("READ", "DRAFT"),
        send_requires_confirmation=True,
        service_domain="mail.qq.com",
        service_domain_aliases=("wx.mail.qq.com",),
    ),
}

MAILBOX_IDENTITIES: Final = MappingProxyType(_MAILBOXES)


@dataclass(frozen=True)
class RuntimeMailboxContext:
    """Ephemeral binding created by an explicit Edge Profile launch."""

    mailbox_id: str
    profile_directory: str
    hwnd: int | None
    launched_at: float


_RUNTIME_CONTEXTS: dict[str, RuntimeMailboxContext] = {}
_RUNTIME_CONTEXTS_LOCK = Lock()
_MAILBOX_WINDOW_LOCKS = {
    mailbox_id: Lock() for mailbox_id in MAILBOX_IDENTITIES
}
_LAUNCH_RETRY_SECONDS = 30.0
_BACHELOR_NAVIGATION_TIMEOUT_SECONDS = 15.0


def _record_runtime_context(
    identity: MailboxIdentity, hwnd: int | None
) -> RuntimeMailboxContext:
    context = RuntimeMailboxContext(
        mailbox_id=identity.mailbox_id,
        profile_directory=identity.profile_directory,
        hwnd=hwnd,
        launched_at=time.time(),
    )
    with _RUNTIME_CONTEXTS_LOCK:
        _RUNTIME_CONTEXTS[identity.mailbox_id] = context
    return context


def get_runtime_mailbox_context(
    mailbox_id: str,
) -> RuntimeMailboxContext | None:
    """Return the current process-only launch binding for one mailbox."""
    with _RUNTIME_CONTEXTS_LOCK:
        return _RUNTIME_CONTEXTS.get(mailbox_id)


def _clear_runtime_mailbox_contexts() -> None:
    """Clear process-only state for deterministic tests."""
    with _RUNTIME_CONTEXTS_LOCK:
        _RUNTIME_CONTEXTS.clear()


def _normalize_edge_title(value: str) -> str:
    return value.translate({
        ord("\u200b"): None,
        ord("\u200c"): None,
        ord("\u200d"): None,
        ord("\u2060"): None,
        ord("\ufeff"): None,
    })


def _visible_edge_windows() -> dict[int, str]:
    windows: dict[int, str] = {}

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        if "microsoft edge" in _normalize_edge_title(title).casefold():
            windows[hwnd] = title

    win32gui.EnumWindows(collect, None)
    return windows


def _context_window_is_valid(context: RuntimeMailboxContext | None) -> bool:
    return bool(
        context is not None
        and context.hwnd is not None
        and win32gui.IsWindow(context.hwnd)
        and win32gui.IsWindowVisible(context.hwnd)
        and context.hwnd in _visible_edge_windows()
    )


def _get_process_command_line(pid: int) -> str:
    """Read a process command line without logging or retaining its content."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3.0,
        creationflags=creationflags,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _profile_directory_from_command_line(command_line: str) -> str | None:
    match = re.search(
        r'--profile-directory(?:=|\s+)(?:"([^"]+)"|(\S+))',
        command_line,
        re.IGNORECASE,
    )
    if not match:
        return None
    return (match.group(1) or match.group(2)).strip()


def _profile_directory_for_hwnd(hwnd: int) -> str | None:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    command_line = _get_process_command_line(pid)
    try:
        return _profile_directory_from_command_line(command_line)
    finally:
        # Command lines may contain a URL. Do not retain or log it.
        del command_line


def _window_title_matches_profile(
    identity: MailboxIdentity, title: str
) -> bool:
    """Match Edge's browser-chrome Profile suffix, not page UIA content."""
    normalized = _normalize_edge_title(title).casefold()
    expected_suffix = (
        f" - {identity.display_name} - Microsoft Edge".casefold()
    )
    return normalized.endswith(expected_suffix)


def _read_edge_service_domain(hwnd: int) -> str | None:
    """Return only the address-bar hostname, discarding the complete URL."""
    def read_hostname() -> str | None:
        window = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
        for control in window.descendants():
            try:
                info = control.element_info
                name = (info.name or "").casefold()
                automation_id = (info.automation_id or "").casefold()
                if (info.control_type or "").casefold() != "edit":
                    continue
                if not (
                    automation_id in {"view_1021", "view_1022"}
                    or any(token in name for token in (
                        "address and search", "地址和搜索栏",
                        "adresse et recherche", "address bar",
                    ))
                ):
                    continue
                address = control.get_value().strip()
                from urllib.parse import urlsplit
                parsed = urlsplit(
                    address if "://" in address else f"https://{address}"
                )
                hostname = (parsed.hostname or "").casefold() or None
                del address
                return hostname
            except Exception:
                continue
        return None

    return _run_bounded(read_hostname, 5.0, "reading Edge service hostname")


def _domain_matches_identity(
    identity: MailboxIdentity, domain: str | None
) -> bool:
    if not domain:
        return False
    accepted = (identity.service_domain, *identity.service_domain_aliases)
    normalized = domain.casefold().rstrip(".")
    return any(
        normalized == expected.casefold().rstrip(".") for expected in accepted
    )


def _find_existing_profile_window(
    identity: MailboxIdentity,
    require_service_domain: bool = False,
    excluded_handles: set[int] | None = None,
) -> int | None:
    excluded = excluded_handles or set()
    candidates = []
    for hwnd, title in _visible_edge_windows().items():
        if hwnd in excluded:
            continue
        try:
            parsed_profile = _profile_directory_for_hwnd(hwnd)
            if (
                parsed_profile == identity.profile_directory
                or (
                    parsed_profile is None
                    and _window_title_matches_profile(identity, title)
                )
            ):
                candidates.append(hwnd)
        except Exception:
            continue
    if not candidates:
        return None
    for hwnd in candidates:
        try:
            if _domain_matches_identity(
                identity, _read_edge_service_domain(hwnd)
            ):
                return hwnd
        except Exception:
            continue
    return None if require_service_domain else candidates[0]


def _navigate_bachelor_window_if_needed(
    identity: MailboxIdentity,
    hwnd: int,
    current_domain: str | None = None,
) -> None:
    """Navigate an already-bound bachelor window to its fixed entry URL."""
    if identity.mailbox_id != "bachelor_mail" or identity.stable_url is None:
        return

    domain = current_domain
    if domain is None:
        try:
            domain = _read_edge_service_domain(hwnd)
        except Exception:
            domain = None
    if _domain_matches_identity(identity, domain):
        return

    confirm_mailbox_identity(identity.mailbox_id, identity.profile_directory)
    _focus_window_handle(hwnd)
    hotkey(["ctrl", "l"])
    type_text(identity.stable_url, interval=0.02)
    press_key("enter")

    deadline = time.monotonic() + _BACHELOR_NAVIGATION_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            domain = _read_edge_service_domain(hwnd)
            if _domain_matches_identity(identity, domain):
                return
        except Exception as error:
            last_error = error
        time.sleep(0.5)

    if last_error is not None:
        raise TimeoutError(
            f"Bachelor mailbox window did not reach "
            f"{identity.service_domain}"
        ) from last_error
    raise TimeoutError(
        f"Bachelor mailbox window did not reach {identity.service_domain}"
    )


def _prepare_existing_mailbox_window(
    identity: MailboxIdentity,
    hwnd: int,
    current_domain: str | None = None,
) -> None:
    if identity.mailbox_id == "bachelor_mail":
        _navigate_bachelor_window_if_needed(
            identity, hwnd, current_domain=current_domain
        )


def _wait_for_launched_edge_window(
    previous_handles: set[int], timeout: float = 8.0
) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _visible_edge_windows()
        new_handles = [hwnd for hwnd in current if hwnd not in previous_handles]
        if new_handles:
            return new_handles[0]
        time.sleep(0.2)
    return None


def confirm_mailbox_identity(
    mailbox_id: str, profile_directory: str
) -> MailboxIdentity:
    """Confirm an operation targets the configured mailbox and Edge profile."""
    identity = MAILBOX_IDENTITIES.get(mailbox_id)
    if identity is None:
        raise ValueError(f"Unknown mailbox identity: {mailbox_id}")
    if identity.profile_directory != profile_directory:
        raise ValueError(
            f"Profile mismatch for {mailbox_id}: expected "
            f"{identity.profile_directory}, got {profile_directory}"
        )
    return identity


def _find_edge_executable() -> str:
    discovered = shutil.which("msedge") or shutil.which("msedge.exe")
    if discovered:
        return discovered

    roots = (
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("LOCALAPPDATA"),
    )
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("Microsoft Edge executable was not found")


def _open_mailbox_window(
    identity: MailboxIdentity, edge_executable: str
) -> dict[str, str | None]:
    confirmed = confirm_mailbox_identity(
        identity.mailbox_id, identity.profile_directory
    )
    arguments = [
        edge_executable,
        f"--profile-directory={confirmed.profile_directory}",
        "--new-window",
    ]
    if confirmed.stable_url:
        arguments.append(confirmed.stable_url)

    previous_handles = set(_visible_edge_windows())
    subprocess.Popen(arguments, close_fds=True)
    hwnd = _wait_for_launched_edge_window(previous_handles)
    _record_runtime_context(confirmed, hwnd)
    if hwnd is None:
        raise TimeoutError(
            f"Edge launch did not produce a bindable window for "
            f"{confirmed.profile_directory}"
        )
    return {
        "mailbox_id": confirmed.mailbox_id,
        "display_name": confirmed.display_name,
        "profile_directory": confirmed.profile_directory,
        "status": "CREATED_NEW_WINDOW",
        "stable_url": confirmed.stable_url,
    }


def get_or_open_mailbox_window(
    mailbox_id: str,
) -> dict[str, str | None]:
    """Reuse, restore, or create the single Agent-managed mailbox window."""
    identity = MAILBOX_IDENTITIES.get(mailbox_id)
    if identity is None:
        raise ValueError(f"Unknown mailbox identity: {mailbox_id}")
    lock = _MAILBOX_WINDOW_LOCKS[mailbox_id]
    with lock:
        context = get_runtime_mailbox_context(mailbox_id)
        if _context_window_is_valid(context):
            try:
                current_domain = _read_edge_service_domain(context.hwnd)
            except Exception:
                current_domain = None
            if not _domain_matches_identity(identity, current_domain):
                preferred_hwnd = _find_existing_profile_window(
                    identity,
                    require_service_domain=True,
                    excluded_handles={context.hwnd},
                )
                if preferred_hwnd is not None:
                    _record_runtime_context(identity, preferred_hwnd)
                    _focus_window_handle(preferred_hwnd)
                    return {
                        "mailbox_id": identity.mailbox_id,
                        "display_name": identity.display_name,
                        "profile_directory": identity.profile_directory,
                        "status": "RESTORED_WINDOW_BINDING",
                        "stable_url": identity.stable_url,
                    }
            _prepare_existing_mailbox_window(
                identity, context.hwnd, current_domain=current_domain
            )
            _focus_window_handle(context.hwnd)
            return {
                "mailbox_id": identity.mailbox_id,
                "display_name": identity.display_name,
                "profile_directory": identity.profile_directory,
                "status": "REUSED_EXISTING_WINDOW",
                "stable_url": identity.stable_url,
            }

        recovered_hwnd = _find_existing_profile_window(identity)
        if recovered_hwnd is not None:
            _record_runtime_context(identity, recovered_hwnd)
            _prepare_existing_mailbox_window(identity, recovered_hwnd)
            _focus_window_handle(recovered_hwnd)
            return {
                "mailbox_id": identity.mailbox_id,
                "display_name": identity.display_name,
                "profile_directory": identity.profile_directory,
                "status": "RESTORED_WINDOW_BINDING",
                "stable_url": identity.stable_url,
            }

        if (
            context is not None
            and context.hwnd is None
            and time.time() - context.launched_at < _LAUNCH_RETRY_SECONDS
        ):
            raise TimeoutError(
                f"Edge launch is still pending for {identity.profile_directory}"
            )
        return _open_mailbox_window(identity, _find_edge_executable())


@mcp.tool()
def open_all_mailboxes() -> list[dict[str, str | None]]:
    """Open each configured mailbox in its own confirmed Edge profile window.

    This launch-only tool does not inspect mailbox content or enter credentials.
    A profile without a configured stable URL is opened without guessing a URL.
    """
    results: list[dict[str, str | None]] = []
    for identity in MAILBOX_IDENTITIES.values():
        try:
            results.append(get_or_open_mailbox_window(identity.mailbox_id))
        except Exception as exc:
            results.append({
                "mailbox_id": identity.mailbox_id,
                "display_name": identity.display_name,
                "profile_directory": identity.profile_directory,
                "status": "failed",
                "stable_url": identity.stable_url,
                "error": str(exc),
            })
    return results


__all__ = [
    "MAILBOX_IDENTITIES", "MailboxIdentity", "RuntimeMailboxContext",
    "confirm_mailbox_identity", "get_runtime_mailbox_context",
    "get_or_open_mailbox_window", "open_all_mailboxes",
]
