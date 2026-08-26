"""Fixed mailbox identities and safe Microsoft Edge launch helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Final

import win32gui

from .server import mcp


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
    ),
    "qq_mail": MailboxIdentity(
        mailbox_id="qq_mail",
        display_name="QQ邮箱",
        browser="Microsoft Edge",
        profile_directory="Profile 3",
        service="QQ Mail",
        stable_url="https://mail.qq.com/",
        permissions=("READ",),
        send_requires_confirmation=True,
        service_domain="mail.qq.com",
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


def _visible_edge_windows() -> dict[int, str]:
    windows: dict[int, str] = {}

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        if "microsoft edge" in title.casefold():
            windows[hwnd] = title

    win32gui.EnumWindows(collect, None)
    return windows


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
    status = (
        "opened"
        if confirmed.stable_url
        else "profile_opened_mailbox_url_not_configured"
    )
    return {
        "mailbox_id": confirmed.mailbox_id,
        "display_name": confirmed.display_name,
        "profile_directory": confirmed.profile_directory,
        "status": status,
        "stable_url": confirmed.stable_url,
    }


@mcp.tool()
def open_all_mailboxes() -> list[dict[str, str | None]]:
    """Open each configured mailbox in its own confirmed Edge profile window.

    This launch-only tool does not inspect mailbox content or enter credentials.
    A profile without a configured stable URL is opened without guessing a URL.
    """
    edge_executable = _find_edge_executable()
    results: list[dict[str, str | None]] = []
    for identity in MAILBOX_IDENTITIES.values():
        try:
            results.append(_open_mailbox_window(identity, edge_executable))
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
    "open_all_mailboxes",
]
