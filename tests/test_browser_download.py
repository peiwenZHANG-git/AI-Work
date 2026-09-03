"""Side-effect-free tests for general browser opening and downloads."""

from __future__ import annotations

from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from windows_gui.browser_download import (
    _validate_public_host, download_file, open_in_edge, redact_web_url,
    validate_web_url,
)


class FakeResponse:
    def __init__(
        self, body=b"hello", *, url="https://files.example/report.txt",
        headers=None, status_code=200,
    ):
        self.body = body
        self.url = url
        self.headers = headers or {}
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class UrlValidationTests(unittest.TestCase):
    def test_accepts_https(self):
        self.assertEqual("https://example.com/a", validate_web_url("https://example.com/a"))

    def test_rejects_credentials_local_and_non_https(self):
        for url in (
            "https://u:p@example.com", "https://127.0.0.1/a",
            "https://[::1]/a", "https://[64:ff9b::10.0.0.1]/a",
            "https://localhost/a", "file:///tmp/a", "http://example.com",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_web_url(url)

    def test_rejects_hostname_resolving_to_private_address(self):
        resolver_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443)),
        ]
        with patch.object(
            socket, "getaddrinfo", return_value=resolver_result
        ), self.assertRaises(ValueError):
            download_file(
                "https://rebind.example/a", "D:/not-created",
                transport=lambda *args, **kwargs: self.fail("transport must not run"),
            )

    def test_redacts_query_fragment_and_credentials(self):
        self.assertEqual(
            "https://example.com/report",
            redact_web_url("https://user:secret@example.com/report?token=private#part"),
        )


class ResolvedAddressGuardTests(unittest.TestCase):
    def _resolve(self, addresses):
        result = [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM, 6, "", (address, 443),
            )
            for address in addresses
        ]
        resolver = patch.object(socket, "getaddrinfo", return_value=result)
        resolver.start()
        self.addCleanup(resolver.stop)

    def test_public_ipv4_and_ipv6_addresses_are_allowed(self):
        self._resolve(["1.2.3.4"])
        self.assertEqual(
            "https://example.com/a",
            _validate_public_host("https://example.com/a", allow_http=False),
        )
        self._resolve(["2606:2800:220:1:248:1893:25c8:1946"])
        self.assertEqual(
            "https://example.com/a",
            _validate_public_host("https://example.com/a", allow_http=False),
        )

    def test_private_local_and_transition_addresses_are_rejected(self):
        cases = [
            "127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.255",
            "192.168.1.1", "169.254.1.1", "0.0.0.0", "224.0.0.1",
            "::1", "fe80::1", "fc00::1", "ff02::1", "::",
            "::ffff:10.0.0.1",
            "64:ff9b::a00:1",
            "2002:a00:1::",
            "2001:0::",
        ]
        for address in cases:
            with self.subTest(address=address):
                self._resolve([address])
                with self.assertRaises(ValueError):
                    _validate_public_host("https://example.com/a", allow_http=False)

    def test_any_private_address_in_multi_record_response_is_rejected(self):
        self._resolve(["1.2.3.4", "10.0.0.9"])
        with self.assertRaises(ValueError):
            _validate_public_host("https://example.com/a", allow_http=False)

    def test_public_then_private_resolution_is_rejected(self):
        self._resolve(["1.2.3.4"])
        _validate_public_host("https://example.com/a", allow_http=False)
        self._resolve(["10.0.0.9"])
        with self.assertRaises(ValueError):
            _validate_public_host("https://example.com/a", allow_http=False)

    def test_unresolvable_hostname_fails_closed(self):
        with patch.object(
            socket, "getaddrinfo", side_effect=OSError("dns failure")
        ), self.assertRaises(ValueError):
            _validate_public_host("https://example.com/a", allow_http=False)


class BrowserOpeningTests(unittest.TestCase):
    def test_opens_new_edge_window_with_injected_runner(self):
        calls = []
        resolver_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443)),
        ]
        with patch.object(socket, "getaddrinfo", return_value=resolver_result):
            result = open_in_edge(
                "https://example.com", profile_directory="Profile 2",
                edge_finder=lambda: Path("C:/Edge/msedge.exe"),
                process_runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
        self.assertEqual("OPENED", result["status"])
        self.assertEqual(
            [str(Path("C:/Edge/msedge.exe")), "--new-window", "--profile-directory=Profile 2", "https://example.com"],
            calls[0][0][0],
        )


class DownloadTests(unittest.TestCase):
    def setUp(self):
        resolver_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443)),
        ]
        resolver = patch.object(socket, "getaddrinfo", return_value=resolver_result)
        resolver.start()
        self.addCleanup(resolver.stop)

    def test_downloads_atomically_and_reports_hash(self):
        response = FakeResponse(headers={"Content-Disposition": "attachment; filename=report.txt", "Content-Type": "text/plain; charset=utf-8", "Content-Length": "5"})
        with tempfile.TemporaryDirectory() as directory:
            result = download_file("https://example.com/report", directory, transport=lambda *a, **k: response)
            self.assertEqual(b"hello", Path(result.path).read_bytes())
            self.assertEqual(5, result.size_bytes)
            self.assertEqual("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824", result.sha256)
            self.assertEqual("text/plain", result.content_type)
            self.assertFalse(list(Path(directory).glob("*.part")))
            self.assertTrue(response.closed)

    def test_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "report.txt").write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                download_file("https://example.com/report.txt", directory, transport=lambda *a, **k: FakeResponse())

    def test_rejects_oversized_stream_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                download_file("https://example.com/large.bin", directory, max_bytes=4, transport=lambda *a, **k: FakeResponse(b"12345"))
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_sanitizes_server_filename(self):
        response = FakeResponse(headers={"Content-Disposition": 'attachment; filename="../CON.txt"'})
        with tempfile.TemporaryDirectory() as directory:
            result = download_file("https://example.com/file", directory, transport=lambda *a, **k: response)
            self.assertEqual("_CON.txt", Path(result.path).name)

    def test_redirect_response_to_private_address_is_rejected(self):
        urls = []
        redirect = FakeResponse(
            url="https://example.com/redirect", status_code=302,
            headers={"Location": "https://127.0.0.1/secret"},
        )
        final = FakeResponse()

        def transport(url, **kwargs):
            self.assertFalse(kwargs["allow_redirects"])
            urls.append(url)
            return redirect if len(urls) == 1 else final

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                download_file("https://example.com/redirect", directory, transport=transport)
            self.assertEqual([], list(Path(directory).iterdir()))
        self.assertEqual(["https://example.com/redirect"], urls)


if __name__ == "__main__":
    unittest.main()
