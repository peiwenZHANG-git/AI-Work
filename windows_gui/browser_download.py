"""Safe primitives for opening web pages and downloading public files."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import requests

from .mailboxes import _find_edge_executable
from .server import mcp


DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True)
class DownloadResult:
    path: str
    source_url: str
    final_url: str
    size_bytes: int
    sha256: str
    content_type: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_web_url(url: str, *, allow_http: bool = False) -> str:
    """Validate a public web URL without resolving or fetching it."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL is required")
    if url != url.strip() or any(ord(char) < 32 for char in url):
        raise ValueError("URL contains whitespace or control characters")
    parsed = urlsplit(url)
    allowed = {"https"}
    if allow_http:
        allowed.add("http")
    if parsed.scheme.casefold() not in allowed:
        raise ValueError("only HTTPS URLs are allowed" if not allow_http else "only HTTP(S) URLs are allowed")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must contain a hostname and no embedded credentials")
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("local URLs are not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("private, loopback, and link-local addresses are not allowed")
    return url


def redact_web_url(url: str) -> str:
    """Return a URL without query, fragment, or embedded credentials."""
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{hostname}{port}{parsed.path or '/'}"


def _safe_filename(value: str) -> str:
    value = unquote(value).strip().strip(". ")
    value = _INVALID_FILENAME.sub("_", value)
    if not value or value in {".", ".."}:
        value = "download"
    stem = value.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        value = f"_{value}"
    return value[:240].rstrip(". ") or "download"


def _filename_from_response(response: Any, requested: str | None) -> str:
    if requested:
        return _safe_filename(Path(requested).name)
    disposition = response.headers.get("Content-Disposition", "")
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
    plain = re.search(r'filename="?([^";]+)', disposition, re.IGNORECASE)
    if encoded:
        return _safe_filename(encoded.group(1))
    if plain:
        return _safe_filename(plain.group(1))
    candidate = Path(unquote(urlsplit(response.url).path)).name
    return _safe_filename(candidate or "download")


def open_in_edge(
    url: str,
    *,
    profile_directory: str | None = None,
    edge_finder: Callable[[], str | Path] = _find_edge_executable,
    process_runner: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Open a validated URL in a new Edge window."""
    validated = validate_web_url(url, allow_http=True)
    command = [str(edge_finder()), "--new-window"]
    if profile_directory:
        if any(char in profile_directory for char in '\r\n\x00'):
            raise ValueError("invalid Edge profile directory")
        command.append(f"--profile-directory={profile_directory}")
    command.append(validated)
    process_runner(command, close_fds=True)
    return {"status": "OPENED", "url": validated, "profile_directory": profile_directory}


def download_file(
    url: str,
    destination_directory: str | Path,
    *,
    filename: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = 30.0,
    allow_http: bool = False,
    overwrite: bool = False,
    transport: Callable[..., Any] = requests.get,
) -> DownloadResult:
    """Download one file atomically, with a size cap and no overwrite by default."""
    validated = validate_web_url(url, allow_http=allow_http)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    destination = Path(destination_directory).expanduser().resolve()
    if not destination.is_dir():
        raise ValueError("destination directory must already exist")

    response = transport(validated, stream=True, timeout=timeout_seconds, allow_redirects=True)
    temporary_path: Path | None = None
    try:
        response.raise_for_status()
        final_url = validate_web_url(response.url, allow_http=allow_http)
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as error:
                raise ValueError("invalid Content-Length") from error
            if declared_size < 0 or declared_size > max_bytes:
                raise ValueError("download exceeds the configured size limit")

        target = destination / _filename_from_response(response, filename)
        if target.exists() and not overwrite:
            raise FileExistsError(f"destination already exists: {target.name}")
        digest = hashlib.sha256()
        size = 0
        descriptor, temporary_name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=destination)
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as output:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("download exceeds the configured size limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if overwrite:
            os.replace(temporary_path, target)
        else:
            # A hard-link publish is atomic and fails if another process creates
            # the target after our earlier existence check.
            os.link(temporary_path, target)
            temporary_path.unlink()
        temporary_path = None
        return DownloadResult(
            path=str(target), source_url=validated, final_url=final_url,
            size_bytes=size, sha256=digest.hexdigest(),
            content_type=response.headers.get("Content-Type", "").split(";", 1)[0].strip(),
        )
    finally:
        response.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@mcp.tool()
def open_webpage(url: str) -> dict[str, Any]:
    """Open a public HTTP(S) URL in a new Edge window.

    This launch-only tool does not inspect the page or enter credentials.
    """
    result = open_in_edge(url)
    result["url"] = redact_web_url(result["url"])
    return result


@mcp.tool()
def download_web_file(
    url: str,
    destination_directory: str,
    filename: str = "",
    max_bytes: int = DEFAULT_MAX_BYTES,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Download a public HTTPS file atomically into an existing directory.

    Existing files are preserved unless overwrite is explicitly true. Browser
    cookies and authenticated browser state are never used by this tool.
    """
    result = download_file(
        url, destination_directory, filename=filename or None,
        max_bytes=max_bytes, overwrite=overwrite,
    ).to_dict()
    result["source_url"] = redact_web_url(result["source_url"])
    result["final_url"] = redact_web_url(result["final_url"])
    result["status"] = "DOWNLOADED"
    return result


__all__ = [
    "DEFAULT_MAX_BYTES", "DownloadResult", "download_file",
    "download_web_file", "open_in_edge", "open_webpage", "redact_web_url",
    "validate_web_url",
]
