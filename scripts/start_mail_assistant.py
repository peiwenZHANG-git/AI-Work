"""Start the assistant server; if already running, trigger refresh and open."""

import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


PORT = 8931
URL = f'http://127.0.0.1:{PORT}/'


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(('127.0.0.1', PORT)) == 0


def trigger_refresh() -> None:
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                f'http://127.0.0.1:{PORT}/api/refresh', data=b'', method='POST'
            ),
            timeout=5,
        )
    except OSError:
        pass


def main() -> int:
    if not port_in_use():
        python = sys.executable
        subprocess.Popen(
            [python, str(Path(__file__).with_name('mail_assistant_server.py'))],
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        for _ in range(40):
            if port_in_use():
                break
            time.sleep(0.25)
    trigger_refresh()
    webbrowser.open(URL)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
