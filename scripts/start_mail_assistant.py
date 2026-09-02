"""Start the assistant server; if already running, trigger refresh and open."""

import argparse
import json
import socket
import shlex
import subprocess
import sys
import time
from pathlib import Path
import urllib.request
import webbrowser


PORT = 8931
URL = f'http://127.0.0.1:{PORT}/'
SERVER_SCRIPT = Path(__file__).with_name('mail_assistant_server.py')


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(('127.0.0.1', PORT)) == 0


def trigger_refresh() -> None:
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                f'http://127.0.0.1:{PORT}/api/refresh',
                data=b'{}',
                headers={'Content-Type': 'application/json'},
                method='POST',
            ),
            timeout=5,
        )
    except OSError:
        pass


def find_assistant_pids(
    script_path: Path,
    *,
    runner=subprocess.run,
) -> list[int]:
    """Return Python process IDs whose command line runs this exact server."""
    result = runner(
        [
            'powershell',
            '-NoProfile',
            '-Command',
            (
                "Get-CimInstance -ClassName Win32_Process -Filter "
                "\"Name='python.exe' OR Name='pythonw.exe'\" | "
                'Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress'
            ),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return []
    entries = payload if isinstance(payload, list) else [payload]
    expected = script_path.resolve()
    pids = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            pid = int(entry.get('ProcessId'))
        except (TypeError, ValueError):
            continue
        commandline = str(entry.get('CommandLine') or '')
        try:
            arguments = [item.strip('"') for item in shlex.split(commandline, posix=False)]
        except ValueError:
            continue
        for argument in arguments[1:]:
            try:
                if Path(argument).resolve() == expected:
                    pids.append(pid)
                    break
            except OSError:
                continue
    return sorted(set(pids))


def stop_assistant_pids(pids: list[int], *, runner=subprocess.run) -> None:
    for pid in pids:
        result = runner(
            ['taskkill', '/PID', str(int(pid)), '/F'],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f'could not stop assistant process {pid}')


def wait_assistant_pids(
    script_path: Path, *, timeout_seconds: float = 3.0
) -> list[int]:
    """Boundedly wait for WMI to expose the exact process command line."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pids = find_assistant_pids(script_path)
        if pids:
            return pids
        time.sleep(0.1)
    return find_assistant_pids(script_path)


def server_python_executable() -> Path:
    current = Path(sys.executable)
    pythonw = current.with_name('pythonw.exe')
    return pythonw if pythonw.is_file() else current


def wait_port(closed: bool, *, timeout_seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if port_in_use() != closed:
            return True
        time.sleep(0.1)
    return port_in_use() != closed


def launch_server(no_refresh: bool = False) -> None:
    python = str(server_python_executable())
    command = [python, str(SERVER_SCRIPT)]
    if no_refresh:
        command.append('--no-refresh')
    subprocess.Popen(
        command,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restart', action='store_true', help='stop only this exact assistant process, then start the updated server')
    parser.add_argument('--no-refresh', action='store_true', help='start without reading mailboxes')
    parser.add_argument('--no-open', action='store_true', help='do not open the browser')
    args = parser.parse_args(argv)
    restart_requested = args.restart
    no_refresh = args.no_refresh
    if restart_requested:
        if port_in_use():
            pids = wait_assistant_pids(SERVER_SCRIPT)
            if not pids:
                print('could not identify the exact assistant process', file=sys.stderr)
                return 1
            stop_assistant_pids(pids)
            if not wait_port(closed=True):
                print('assistant port is still in use', file=sys.stderr)
                return 1
        launch_server(no_refresh=True)
        if not wait_port(closed=False):
            print('updated assistant did not start', file=sys.stderr)
            return 1
    else:
        if not port_in_use():
            launch_server(no_refresh=no_refresh)
            for _ in range(40):
                if port_in_use():
                    break
                time.sleep(0.25)
        if not no_refresh:
            trigger_refresh()
    if not args.no_open:
        webbrowser.open(URL)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
