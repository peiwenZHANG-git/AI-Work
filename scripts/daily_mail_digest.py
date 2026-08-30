"""Scheduled-task entry point for the nightly three-mailbox digest."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from windows_gui.mail_digest import main


if __name__ == '__main__':
    raise SystemExit(main())
