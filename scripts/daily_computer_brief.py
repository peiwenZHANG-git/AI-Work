"""Generate the local AI-Work morning brief without an MCP server."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_gui.computer_brief import artifact_dir, run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-notify', action='store_true')
    parser.add_argument('--open', action='store_true', help='manually open the generated HTML')
    args = parser.parse_args(argv)
    if args.dry_run and args.open:
        parser.error('--open cannot be used with --dry-run')
    result = run(dry_run=args.dry_run, no_notify=args.no_notify)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if result['ok'] and args.open:
        import os
        os.startfile(artifact_dir() / 'latest.html')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
