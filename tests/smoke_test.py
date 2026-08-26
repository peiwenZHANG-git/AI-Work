"""Safe, interactive Windows GUI smoke test using a dedicated Notepad file.

Run from the project root with:
    python tests/smoke_test.py

The script creates and saves only uniquely named artifacts, then leaves
Notepad open for visual inspection. It never overwrites or deletes files,
closes programs, or interacts with an existing Notepad document.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, TypeVar

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import pyautogui
    import win32gui
    from pywinauto import Desktop

    from windows_gui.mouse import SCREENSHOT_PATH, screenshot
    from windows_gui.uia import (
        click_control,
        click_menu_item,
        click_save_button,
        list_controls,
        set_save_dialog_filename,
    )
    from windows_gui.windows import (
        focus_window,
        focus_window_and_hotkey,
        focus_window_and_press,
        focus_window_and_scroll,
        focus_window_and_type,
        list_windows,
    )
    from windows_gui_mcp import mcp
    IMPORT_ERROR: BaseException | None = None
except (ImportError, OSError) as error:
    IMPORT_ERROR = error

EXPECTED_TOOLS = {
    "click_control", "click_menu_item", "click_mouse", "click_save_button",
    "double_click", "drag_mouse", "focus_and_press", "focus_window",
    "focus_window_and_hotkey", "focus_window_and_press",
    "focus_window_and_scroll", "focus_window_and_type",
    "get_mouse_position", "hotkey", "list_controls", "list_windows",
    "move_mouse", "press_key", "right_click", "screenshot", "scroll",
    "set_save_dialog_filename", "type_text",
}

T = TypeVar("T")
failures = 0


def pass_result(name: str, detail: str = "") -> None:
    suffix = f" - {detail}" if detail else ""
    print(f"PASS: {name}{suffix}", flush=True)


def fail_result(name: str, error: BaseException | str) -> None:
    global failures
    failures += 1
    print(f"FAIL: {name} - {type(error).__name__}: {error}" if isinstance(error, BaseException)
          else f"FAIL: {name} - {error}", flush=True)


def manual_check(instruction: str) -> None:
    print(f"MANUAL CHECK: {instruction}", flush=True)


def step(name: str, action: Callable[[], T]) -> T | None:
    try:
        result = action()
        if isinstance(result, (list, tuple, set, dict)):
            detail = f"returned {len(result)} item(s)"
        else:
            detail = str(result) if result is not None else ""
        pass_result(name, detail)
        return result
    except Exception as error:
        fail_result(name, error)
        return None


def check_registration() -> str:
    if hasattr(mcp, "list_tools"):
        tools = asyncio.run(mcp.list_tools())
        names = {tool.name for tool in tools}
    else:
        names = set(asyncio.run(mcp.get_tools()))
    missing = EXPECTED_TOOLS - names
    unexpected = names - EXPECTED_TOOLS
    if missing or unexpected:
        raise AssertionError(
            f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )
    return f"registered {len(names)} tools"


def wait_for_notepad_title(unique_name: str, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    last_titles: list[str] = []
    while time.monotonic() < deadline:
        last_titles = list_windows()
        for title in last_titles:
            if unique_name.lower() in title.lower():
                return unique_name
        time.sleep(0.5)
    raise TimeoutError(
        f"Notepad title containing {unique_name!r} was not found; "
        f"visible titles={last_titles}"
    )


def visible_notepad_windows() -> dict[int, str]:
    windows: dict[int, str] = {}

    def collect(hwnd, _):
        if (
            win32gui.IsWindowVisible(hwnd)
            and win32gui.GetClassName(hwnd) == "Notepad"
        ):
            windows[hwnd] = win32gui.GetWindowText(hwnd).strip()

    win32gui.EnumWindows(collect, None)
    return windows


def _is_safe_blank_notepad_title(title: str) -> bool:
    lowered = title.casefold()
    return (
        not title.startswith("*")
        and any(
            token in lowered for token in ("untitled", "无标题", "sans titre")
        )
    )


def wait_for_blank_notepad_transition(
    previous_windows: dict[int, str], timeout: float = 15.0
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for hwnd, title in visible_notepad_windows().items():
            changed = hwnd not in previous_windows or previous_windows[hwnd] != title
            if changed and _is_safe_blank_notepad_title(title):
                return hwnd
        time.sleep(0.25)
    raise TimeoutError(
        "Notepad did not create a confirmed empty untitled tab"
    )


def launch_safe_blank_notepad() -> int:
    previous_windows = visible_notepad_windows()
    smoke_titles = [
        title
        for title in previous_windows.values()
        if any(
            token in title.lower()
            for token in (
                "notepad_smoke_", "saved_smoke_", "mcp_smoke_",
                "scrollable smoke text", "smoke text scrollable",
            )
        )
    ]
    existing_titles = [title for title in previous_windows.values() if title]
    if smoke_titles or existing_titles:
        focus_window_and_hotkey(
            (smoke_titles or existing_titles)[0], "ctrl+shift+n"
        )
    else:
        subprocess.Popen(["notepad.exe"])
    return wait_for_blank_notepad_transition(previous_windows)


def open_save_as_for_hwnd(hwnd: int) -> str:
    Desktop(backend="uia").window(handle=hwnd).set_focus()
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "shift", "s")
    return f"opened Save As for HWND {hwnd}"


def cancel_stale_smoke_dialogs() -> str:
    """Cancel only old dialogs whose own UIA text identifies smoke artifacts."""
    dialog_handles: list[int] = []

    def collect(hwnd, _):
        if (
            win32gui.IsWindowVisible(hwnd)
            and win32gui.GetClassName(hwnd) == "#32770"
        ):
            dialog_handles.append(hwnd)

    win32gui.EnumWindows(collect, None)
    cancelled = 0
    for hwnd in dialog_handles:
        try:
            wrapper = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
            controls = [wrapper, *wrapper.descendants()]
            evidence = []
            for control in controls:
                name = (control.element_info.name or "").strip()
                if name:
                    evidence.append(name)
                if (control.element_info.control_type or "") == "Edit":
                    try:
                        evidence.append(control.get_value())
                    except Exception:
                        pass
            combined = " ".join(evidence).lower()
            if not any(
                token in combined
                for token in (
                    "smoke_artifacts", "notepad_smoke_", "saved_smoke_",
                    "mcp_smoke_",
                )
            ):
                continue
            for control in controls:
                if (control.element_info.control_type or "") != "Button":
                    continue
                name = (control.element_info.name or "").strip().lower()
                if name.startswith(("否", "no", "non", "取消", "cancel", "annuler")):
                    control.click_input()
                    cancelled += 1
                    break
        except Exception:
            continue
    return f"cancelled {cancelled} stale smoke dialog(s)"


def seed_unique_blank_title(hwnd: int, marker: str) -> str:
    Desktop(backend="uia").window(handle=hwnd).set_focus()
    time.sleep(0.3)
    pyautogui.write(marker, interval=0.01)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        title = win32gui.GetWindowText(hwnd).strip()
        if marker.lower() in title.lower():
            return title
        time.sleep(0.1)
    raise TimeoutError(f"Notepad title did not include setup marker: {marker}")


def wait_for_save_dialog(timeout: float = 10.0) -> str:
    expected = ("save as", "另存为", "enregistrer sous")
    deadline = time.monotonic() + timeout
    last_titles: list[str] = []
    while time.monotonic() < deadline:
        last_titles = list_windows()
        for title in last_titles:
            if any(token in title.lower() for token in expected):
                return title
        time.sleep(0.25)
    raise TimeoutError(f"Save As dialog was not found; visible titles={last_titles}")


def wait_for_file(path: Path, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return f"created {path} ({path.stat().st_size} bytes)"
        time.sleep(0.25)
    raise TimeoutError(f"Saved smoke-test file was not created: {path}")


def choose_safe_control(controls: list[dict]) -> tuple[str, str]:
    for preferred_type in ("Document", "Edit"):
        for item in controls:
            name = (item.get("name") or "").strip()
            control_type = (item.get("control_type") or "").strip()
            if name and control_type.lower() == preferred_type.lower():
                return name, control_type
    raise LookupError("No named Document or Edit control was exposed by Notepad")


def run_uia_worker(action: str, *arguments: str):
    command = [sys.executable, str(Path(__file__).resolve()), "--uia-worker", action, *arguments]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    output = completed.stdout.strip()
    if completed.returncode != 0:
        reason = completed.stderr.strip() or output or f"exit code {completed.returncode}"
        raise RuntimeError(f"isolated UIA worker failed: {reason}")
    try:
        payload = json.loads(output.splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid UIA worker output: {output!r}") from error
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error", "unknown UIA worker error"))
    return payload.get("result")


def exercise_safe_menu(window_title: str, controls: list[dict]) -> str:
    names = {(item.get("name") or "").strip().lower() for item in controls}
    # A new empty tab is confined to the dedicated Notepad instance and does
    # not save, delete, overwrite, or close any data.
    candidates = (
        ("File", "New tab"),
        ("文件", "新建标签页"),
        ("Fichier", "Nouvel onglet"),
    )
    errors = []
    matching = [pair for pair in candidates if pair[0].lower() in names]
    ordered = matching or list(candidates)
    for menu_name, item_name in ordered:
        try:
            return run_uia_worker("click_menu_item", window_title, menu_name, item_name)
        except Exception as error:
            errors.append(f"{menu_name}/{item_name}: {error}")
    raise LookupError("No supported safe Notepad menu pair worked; " + "; ".join(errors))


def capture_screenshot_evidence(artifact_dir: Path) -> str:
    original = SCREENSHOT_PATH.read_bytes() if SCREENSHOT_PATH.exists() else None
    try:
        result = screenshot()
        generated = Path(str(result.path))
        if not generated.is_file() or generated.stat().st_size == 0:
            raise AssertionError(f"Screenshot was not created: {generated}")
        evidence = artifact_dir / f"screenshot_{int(time.time())}.png"
        shutil.copy2(generated, evidence)
        return str(evidence)
    finally:
        if original is not None:
            SCREENSHOT_PATH.write_bytes(original)


def main() -> int:
    print("Windows GUI MCP safe smoke test", flush=True)
    print(
        "Safety: unique smoke artifacts only; no overwrite, delete, close, or messaging actions.",
        flush=True,
    )

    if IMPORT_ERROR is not None:
        fail_result(
            "Import Windows GUI dependencies",
            f"{IMPORT_ERROR}. Install fastmcp, pyautogui, pywin32, and pywinauto "
            "in the Python interpreter used by this command.",
        )
        return 1
    pass_result("Import Windows GUI dependencies")

    step("FastMCP tool registration", check_registration)

    artifact_dir = PROJECT_ROOT / "tests" / "smoke_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    fixture = artifact_dir / f"notepad_smoke_{int(time.time())}.txt"
    pass_result("Reserve dedicated Notepad fixture path", str(fixture))
    step("cancel stale smoke-only dialogs", cancel_stale_smoke_dialogs)

    new_hwnd = step(
        "launch and identify safe blank Notepad tab",
        launch_safe_blank_notepad,
    )
    if new_hwnd is None:
        return 1
    setup_marker = f"MCP_SMOKE_SETUP_{int(time.time())}"
    seeded_title = step(
        "seed unique title in safe blank Notepad",
        lambda: seed_unique_blank_title(new_hwnd, setup_marker),
    )
    if seeded_title is None:
        return 1

    window_title = step(
        "list_windows finds dedicated Notepad",
        lambda: wait_for_notepad_title(setup_marker),
    )
    if window_title is None:
        return 1

    step("focus_window", lambda: focus_window(window_title))
    evidence = step("screenshot", lambda: capture_screenshot_evidence(artifact_dir))
    if evidence:
        manual_check(f"Open {evidence} and confirm the dedicated Notepad is visible.")

    controls = step("list_controls", lambda: run_uia_worker("list_controls", window_title))
    if controls is None:
        controls = []
    elif not controls:
        fail_result("list_controls content", "Notepad returned no useful controls")
    else:
        pass_result("list_controls content", f"returned {len(controls)} controls")

    if controls:
        def click_safe_editor() -> str:
            name, control_type = choose_safe_control(controls)
            return run_uia_worker("click_control", window_title, name, control_type)
        step("click_control on Notepad editor", click_safe_editor)
    else:
        fail_result("click_control on Notepad editor", "prerequisite list_controls failed")

    marker = f" [MCP_SMOKE_{int(time.time())}] "
    smoke_text = marker + ("scrollable smoke text " * 200)
    typed = step(
        "focus_window_and_type",
        lambda: focus_window_and_type(window_title, smoke_text, interval=0.001),
    )
    if typed:
        current_title = win32gui.GetWindowText(new_hwnd).strip()
        if not current_title:
            fail_result("refresh Notepad title after typing", "target HWND has no title")
        else:
            window_title = current_title
            pass_result("refresh Notepad title after typing", window_title)
    manual_check(f"Confirm Notepad visibly contains the unsaved marker {marker!r}.")

    step(
        "focus_window_and_press",
        lambda: focus_window_and_press(window_title, "home"),
    )
    manual_check("Confirm the caret moved to the start of the current line.")

    step(
        "focus_window_and_hotkey",
        lambda: focus_window_and_hotkey(window_title, "ctrl+a"),
    )
    manual_check("Confirm the Notepad document text is selected.")

    # Clear the selection without editing, then scroll the long fixture.
    step(
        "focus_window_and_press after hotkey",
        lambda: focus_window_and_press(window_title, "right"),
    )
    step(
        "focus_window_and_scroll",
        lambda: focus_window_and_scroll(window_title, -3),
    )
    manual_check("Confirm Notepad scrolled down slightly and remains open with unsaved edits.")

    save_target = fixture
    step(
        "open dedicated Save As dialog",
        lambda: focus_window_and_hotkey(window_title, "ctrl+shift+s"),
    )
    save_dialog_title = step("find dedicated Save As dialog", wait_for_save_dialog)
    if save_dialog_title:
        step(
            "set_save_dialog_filename",
            lambda: run_uia_worker(
                "set_save_dialog_filename", save_dialog_title, str(save_target)
            ),
        )
        save_clicked = step(
            "click_save_button",
            lambda: run_uia_worker("click_save_button", save_dialog_title, "保存"),
        )
        if save_clicked:
            saved = step("verify dedicated saved file", lambda: wait_for_file(save_target))
            if saved:
                window_title = save_target.name
                manual_check(
                    f"Confirm Notepad now shows the dedicated saved file {save_target.name!r}."
                )
    else:
        fail_result("set_save_dialog_filename", "prerequisite Save As dialog failed")
        fail_result("click_save_button", "prerequisite Save As dialog failed")

    if controls:
        step("click_menu_item safe New tab", lambda: exercise_safe_menu(window_title, controls))
        manual_check("Confirm only a harmless empty Notepad tab was added.")
    else:
        fail_result("click_menu_item safe New tab", "prerequisite list_controls failed")

    print("Notepad is intentionally left open; do not save the smoke-test edits.", flush=True)
    if failures:
        print(f"FAIL: smoke test completed with {failures} failed step(s).", flush=True)
        return 1
    print("PASS: smoke test completed with no automated failures.", flush=True)
    return 0


def uia_worker(arguments: list[str]) -> int:
    def emit(payload: dict) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    try:
        action, *values = arguments
        if action == "list_controls":
            result = list_controls(values[0])
        elif action == "click_control":
            result = click_control(values[0], values[1], values[2])
        elif action == "click_menu_item":
            result = click_menu_item(values[0], values[1], values[2])
        elif action == "set_save_dialog_filename":
            result = set_save_dialog_filename(values[0], values[1])
        elif action == "click_save_button":
            result = click_save_button(values[0], values[1])
        else:
            raise ValueError(f"unsupported worker action: {action}")
        emit({"ok": True, "result": result})
        return 0
    except Exception as error:
        emit({"ok": False, "error": f"{type(error).__name__}: {error}"})
        return 1


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--uia-worker":
        raise SystemExit(uia_worker(sys.argv[2:]))
    raise SystemExit(main())
