"""Command-line entry point for safe browser opening and file downloads."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from windows_gui.browser_download import DEFAULT_MAX_BYTES, download_file, open_in_edge


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open a web page or download one public file safely")
    commands = parser.add_subparsers(dest="command", required=True)
    open_parser = commands.add_parser("open", help="open a URL in a new Edge window")
    open_parser.add_argument("url")
    open_parser.add_argument("--profile-directory")

    download_parser = commands.add_parser("download", help="download a file atomically")
    download_parser.add_argument("url")
    download_parser.add_argument("destination", type=Path)
    download_parser.add_argument("--filename")
    download_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    download_parser.add_argument("--timeout", type=float, default=30.0)
    download_parser.add_argument("--allow-http", action="store_true")
    download_parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "open":
        result = open_in_edge(arguments.url, profile_directory=arguments.profile_directory)
    else:
        result = download_file(
            arguments.url, arguments.destination, filename=arguments.filename,
            max_bytes=arguments.max_bytes, timeout_seconds=arguments.timeout,
            allow_http=arguments.allow_http, overwrite=arguments.overwrite,
        ).to_dict()
        result["status"] = "DOWNLOADED"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
