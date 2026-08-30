"""One-time interactive login that stores the master Graph refresh token."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from windows_gui.mail_digest import ensure_environment
from windows_gui.master_oauth import bootstrap_master_login


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--port', type=int, default=8932)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args(argv)
    ensure_environment()
    result = bootstrap_master_login(
        open_browser=not args.no_open,
        port=args.port,
        timeout_seconds=args.timeout,
    )
    if result.get('stored_refresh_token'):
        print('Outlook refresh token 已保存到 Windows Credential Manager。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
