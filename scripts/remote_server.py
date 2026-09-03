"""CLI entry for the loopback-only AI-Work Remote API server."""

from __future__ import annotations

import argparse
import sys
import threading

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from windows_gui.remote.server import DEFAULT_PORT, RemoteServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Run the loopback-only AI-Work Remote API server',
    )
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    arguments = parser.parse_args(argv)
    server = RemoteServer(port=arguments.port)
    server.start()
    print(f'AI-Work Remote API listening on 127.0.0.1:{server.port}', flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
