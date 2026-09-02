"""Side-effect-free tests for general browser opening and downloads."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from windows_gui.browser_download import (
    download_file, open_in_edge, redact_web_url, validate_web_url,
)


class FakeResponse:
    def __init__(self, body=b"hello", *, url="https://files.example/report.txt", headers=None):
        self.body = body
        self.url = url
        self.headers = headers or {}
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
        for url in ("https://u:p@example.com", "https://127.0.0.1/a", "https://localhost/a", "file:///tmp/a", "http://example.com"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_web_url(url)

    def test_redacts_query_fragment_and_credentials(self):
        self.assertEqual(
            "https://example.com/report",
            redact_web_url("https://user:secret@example.com/report?token=private#part"),
        )


class BrowserOpeningTests(unittest.TestCase):
    def test_opens_new_edge_window_with_injected_runner(self):
        calls = []
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


if __name__ == "__main__":
    unittest.main()
