"""Win32 tray icon and fixed Win+Alt+A entry point for the local assistant."""

from __future__ import annotations

import threading
import webbrowser
from typing import Callable


HOTKEY_ID = 0xA17A
WM_TRAY = 0x8000 + 20
MENU_OPEN = 1001
MENU_RECENT = 1002
MENU_EXIT = 1003


class HotkeyUnavailable(RuntimeError):
    pass


class NativeTrayAdapter:
    """Own a message-only window, notification icon, and global hotkey."""

    def __init__(self) -> None:
        self._thread = None
        self._hwnd = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error = None

    def start(self, on_open, on_recent, on_exit) -> None:
        self._callbacks = (on_open, on_recent, on_exit)
        self._thread = threading.Thread(target=self._run, name='ai-work-tray', daemon=True)
        self._thread.start()
        self._ready.wait(5.0)
        if self._error:
            raise self._error

    def _run(self) -> None:
        try:
            import win32api
            import win32con
            import win32gui

            def window_proc(hwnd, message, wparam, lparam):
                if message == win32con.WM_HOTKEY and wparam == HOTKEY_ID:
                    self._callbacks[0]()
                    return 0
                if message == WM_TRAY and lparam in (win32con.WM_RBUTTONUP, win32con.WM_LBUTTONDBLCLK):
                    if lparam == win32con.WM_LBUTTONDBLCLK:
                        self._callbacks[0]()
                        return 0
                    menu = win32gui.CreatePopupMenu()
                    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_OPEN, 'Open AI-Work')
                    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_RECENT, 'Open recent tasks')
                    win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, '')
                    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_EXIT, 'Exit AI-Work')
                    x, y = win32gui.GetCursorPos()
                    win32gui.SetForegroundWindow(hwnd)
                    command = win32gui.TrackPopupMenu(
                        menu, win32con.TPM_LEFTALIGN | win32con.TPM_RETURNCMD,
                        x, y, 0, hwnd, None,
                    )
                    win32gui.DestroyMenu(menu)
                    # Keep later notification-area menus responsive after dismissal.
                    win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
                    if command == MENU_OPEN:
                        self._callbacks[0]()
                    elif command == MENU_RECENT:
                        self._callbacks[1]()
                    elif command == MENU_EXIT:
                        self._callbacks[2]()
                        win32gui.PostQuitMessage(0)
                    return 0
                if message == win32con.WM_CLOSE:
                    win32gui.DestroyWindow(hwnd)
                    return 0
                return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

            instance = win32api.GetModuleHandle(None)
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = instance
            window_class.lpszClassName = f'AIWorkTray-{id(self)}'
            window_class.lpfnWndProc = window_proc
            atom = win32gui.RegisterClass(window_class)
            hwnd = win32gui.CreateWindow(atom, 'AI-Work', 0, 0, 0, 0, 0, 0, 0, instance, None)
            self._hwnd = hwnd
            icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_ADD,
                (hwnd, 0, win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
                 WM_TRAY, icon, 'AI-Work'),
            )
            if not win32gui.RegisterHotKey(hwnd, HOTKEY_ID, win32con.MOD_WIN | win32con.MOD_ALT, ord('A')):
                # Keep the tray usable and surface one fixed error to the host.
                self._error = HotkeyUnavailable('Win+Alt+A is already in use.')
                win32gui.Shell_NotifyIcon(
                    win32gui.NIM_MODIFY,
                    (hwnd, 0, win32gui.NIF_INFO, WM_TRAY, icon, 'AI-Work',
                     'Win+Alt+A is already in use. Open AI-Work from the tray menu.',
                     5000, 'AI-Work hotkey unavailable', win32gui.NIIF_WARNING),
                )
            self._ready.set()
            win32gui.PumpMessages()
        except Exception as error:
            self._error = error
            self._ready.set()
        finally:
            try:
                import win32gui
                if self._hwnd:
                    win32gui.UnregisterHotKey(self._hwnd, HOTKEY_ID)
                    win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self._hwnd, 0))
            except Exception:
                pass
            self._hwnd = None

    def stop(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            import win32con
            import win32gui
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout=5.0)


class TrayController:
    def __init__(
        self,
        *,
        adapter=None,
        opener: Callable[[str], object] = webbrowser.open,
        shutdown: Callable[[], object] = lambda: None,
        base_url: str = 'http://127.0.0.1:8931/',
    ) -> None:
        self.adapter = adapter or NativeTrayAdapter()
        self.opener = opener
        self.shutdown = shutdown
        self.base_url = base_url

    def start(self) -> None:
        self.adapter.start(
            lambda: self.opener(self.base_url),
            lambda: self.opener(self.base_url + '#recent-tasks'),
            self.shutdown,
        )

    def stop(self) -> None:
        self.adapter.stop()


__all__ = ['HOTKEY_ID', 'HotkeyUnavailable', 'NativeTrayAdapter', 'TrayController']
