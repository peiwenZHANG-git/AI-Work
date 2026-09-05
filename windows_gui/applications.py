"""Allowlisted application launch and safe local document opening; no shell strings."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Callable

import win32api
from pydantic import SkipValidation
from win32com.shell import shell

from . import local_paths as paths
from .server import mcp


APP_ALIASES = frozenset({'notepad', 'calculator', 'explorer', 'edge', 'vscode'})
DOCUMENT_TYPES = frozenset({'.pdf', '.txt', '.md', '.png', '.jpg', '.jpeg', '.bmp'})


def resolve_app(alias: str) -> Path:
    system = Path(win32api.GetSystemDirectory())
    if alias == 'notepad':
        candidates = [system / 'notepad.exe']
    elif alias == 'calculator':
        candidates = [system / 'calc.exe']
    elif alias == 'explorer':
        candidates = [Path(win32api.GetWindowsDirectory()) / 'explorer.exe']
    elif alias == 'edge':
        candidates = [paths.known_folder(name) / 'Microsoft/Edge/Application/msedge.exe'
                      for name in ('ProgramFilesX86', 'ProgramFiles')]
    elif alias == 'vscode':
        candidates = [paths.known_folder('LocalAppData') / 'Programs/Microsoft VS Code/Code.exe',
                      paths.known_folder('ProgramFiles') / 'Microsoft VS Code/Code.exe']
    else:
        raise paths.PathError('unknown_app')
    for candidate in candidates:
        try:
            with paths.pinned(candidate, allow_links=True) as lease:
                paths.require_file(lease)
                return candidate
        except Exception as error:
            if paths.error_result(error)['code'] != 'not_found':
                raise
    raise paths.PathError('app_not_installed')


def _launch(executable: Path, arguments: tuple[str, ...] = ()) -> None:
    subprocess.Popen([str(executable), *arguments], executable=str(executable),
                     shell=False, cwd=str(executable.parent), close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def launch_app(alias: str, *, resolver: Callable = resolve_app,
               launcher: Callable = _launch) -> dict:
    try:
        if not isinstance(alias, str) or alias not in APP_ALIASES:
            raise paths.PathError('unknown_app')
        executable = resolver(alias)
        with paths.pinned(executable, allow_links=True) as lease:
            paths.require_file(lease)
            launcher(executable)
        return {'status': 'ok', 'code': 'launch_requested', 'app': alias}
    except Exception as error:
        return paths.error_result(error)


def _open_document(path: Path) -> None:
    # Windows association API, not cmd.exe / a shell command string. NOASYNC
    # keeps the lease for association dispatch, not for the application's lifetime.
    shell.ShellExecuteEx(fMask=0x100 | 0x400, lpVerb='open', lpFile=str(path), nShow=1)


def open_local(path: str, *, policy: paths.PathPolicy | None = None,
               document_launcher: Callable = _open_document,
               app_launcher: Callable = _launch,
               resolver: Callable = resolve_app) -> dict:
    try:
        policy = policy or paths.PathPolicy()
        target = policy.target(path)
        with paths.pinned(target.path) as lease:
            if lease.directory:
                executable = resolver('explorer')
                with paths.pinned(executable, allow_links=True):
                    app_launcher(executable, (str(target.path),))
            else:
                if target.path.suffix.lower() not in DOCUMENT_TYPES:
                    raise paths.PathError('document_type_not_allowed')
                document_launcher(target.path)
        return {'status': 'ok', 'code': 'open_requested', 'path': target.label}
    except Exception as error:
        return paths.error_result(error)


@mcp.tool()
def open_path(path: SkipValidation[str]) -> dict:
    """Open an allowed local directory or PDF/text/image document in Downloads/Documents.

    Paths can use Downloads/name or Documents/name. Executables, scripts, shortcuts,
    reparse points and other file types are rejected. Returns dispatch, not viewer readiness.
    """
    return open_local(path)


@mcp.tool()
def open_app(app: SkipValidation[str]) -> dict:
    """Launch a fixed installed alias: notepad, calculator, explorer, edge, vscode.

    No executable paths, arguments, URLs or interpreters. Returns launch requested,
    not a claim that an application window is ready.
    """
    return launch_app(app)
