import asyncio
import json
import socket
import unittest
from unittest import mock

import research_sources as rs


class FakeResponse:
    def __init__(self, body=b"", *, status=200, headers=None, url="https://example.org/x"):
        self._body = body
        self.content = self
        self.status = status
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.url = url
        self.closed = False

    async def read(self, n=-1):
        if n < 0:
            return self._body
        data, self._body = self._body[:n], self._body[n:]
        return data

    def release(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.closed = False
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise RuntimeError("unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self):
        self.closed = True


class URLValidationTests(unittest.TestCase):
    def test_rejects_local_and_credentialed_urls(self):
        bad = [
            "file:///etc/passwd", "http://localhost/a", "http://127.0.0.1/a",
            "http://[::1]/a", "http://100.64.0.1/a", "https://u:p@example.org/a",
            "http://169.254.169.254/latest/meta-data/",
        ]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(rs.SourceError):
                rs._validate_url(url)

    def test_accepts_public_http_url(self):
        self.assertEqual(rs._validate_url("https://example.org/a"), "https://example.org/a")

    def test_rejects_ipv4_and_ipv6_multicast_literals(self):
        for url in ["http://224.0.0.1/a", "http://239.255.255.250/a", "http://[ff02::1]/a"]:
            with self.subTest(url=url), self.assertRaises(rs.SourceError):
                rs._validate_url(url)


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_when_any_resolved_address_is_non_public(self):
        async def mixed(*args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443)),
            ]

        resolver = rs.SafeResolver(getaddrinfo=mixed)
        with self.assertRaises(OSError):
            await resolver.resolve("example.org", 443)

    async def test_normalizes_public_resolver_results(self):
        async def public(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        result = await rs.SafeResolver(getaddrinfo=public).resolve("example.org", 443)
        self.assertEqual(result[0]["host"], "93.184.216.34")

    async def test_rejects_ipv4_and_ipv6_multicast_dns_answers(self):
        for family, address in [(socket.AF_INET, "224.0.0.1"), (socket.AF_INET6, "ff02::1")]:
            async def multicast(*args, _family=family, _address=address, **kwargs):
                sockaddr = (_address, 443) if _family == socket.AF_INET else (_address, 443, 0, 0)
                return [(_family, socket.SOCK_STREAM, 6, "", sockaddr)]

            with self.subTest(address=address), self.assertRaises(OSError):
                await rs.SafeResolver(getaddrinfo=multicast).resolve("example.org", 443)


class ExtractionTests(unittest.TestCase):
    def test_html_extraction_removes_noise_and_finds_title(self):
        html = b"""<html><head><title>  A Paper </title><style>x</style></head>
        <body><nav>menu</nav><article><h1>A Paper</h1><p>Useful evidence.</p></article>
        <script>secret()</script></body></html>"""
        title, text = rs._extract_html(html, "utf-8")
        self.assertEqual(title, "A Paper")
        self.assertIn("Useful evidence.", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("menu", text)


class PDFExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_parser_streams_stdout_and_stops_at_text_limit(self):
        class ExpandingStdout:
            async def read(self, size):
                if process.returncode is not None:
                    return b""
                return b"x" * size

        class Process:
            def __init__(self):
                self.stdout = ExpandingStdout()
                self.returncode = None
                self.terminated = False

            def kill(self):
                self.terminated = True
                self.returncode = -15

            async def wait(self):
                return self.returncode or 0

        process = Process()
        with mock.patch.object(rs.shutil, "which", return_value="/usr/bin/pdftotext"), \
             mock.patch.object(rs.asyncio, "create_subprocess_exec", mock.AsyncMock(return_value=process)) as create:
            text = await rs._pdf_text(b"%PDF-small-input")
        self.assertEqual(len(text), rs.MAX_TEXT_CHARS)
        self.assertTrue(process.terminated)
        args, kwargs = create.call_args
        self.assertEqual(args[-1], "-")
        self.assertEqual(kwargs["stdout"], asyncio.subprocess.PIPE)

    async def test_pdf_parser_cancellation_kills_and_reaps_child(self):
        started = asyncio.Event()
        released = asyncio.Event()

        class BlockingStdout:
            async def read(self, size):
                started.set()
                await released.wait()
                return b""

        class Process:
            def __init__(self):
                self.stdout = BlockingStdout()
                self.returncode = None
                self.killed = False
                self.reaped = False

            def kill(self):
                self.killed = True
                self.returncode = -9
                released.set()

            async def wait(self):
                self.reaped = True
                return self.returncode or 0

        process = Process()
        with mock.patch.object(rs.shutil, "which", return_value="/usr/bin/pdftotext"), \
             mock.patch.object(rs.asyncio, "create_subprocess_exec", mock.AsyncMock(return_value=process)):
            task = asyncio.create_task(rs._pdf_text(b"%PDF-small-input"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.killed)
        self.assertTrue(process.reaped)


class PublicSourcesTests(unittest.IsolatedAsyncioTestCase):
    async def test_hugging_face_nested_paper_schema_is_normalized(self):
        payload = [{"title": "Agent Science", "summary": "A provider abstract.",
                    "paper": {"id": "2502.18864"}}]
        session = FakeSession([FakeResponse(json.dumps(payload).encode(),
                                            headers={"Content-Type": "application/json"})])
        sources = rs.PublicSources(session_factory=lambda **kw: session)
        result = await sources._hf_search("agents", 1)
        self.assertEqual(result[0]["url"], "https://huggingface.co/papers/2502.18864")
        self.assertIn("Abstract/provider metadata only", result[0]["snippet"])

    async def test_fetch_follows_valid_redirect_and_extracts_html(self):
        session = FakeSession([
            FakeResponse(status=302, headers={"Location": "/paper"}, url="https://example.org/start"),
            FakeResponse(b"<title>T</title><main>Full text</main>", url="https://example.org/paper"),
        ])
        async with rs.PublicSources(session_factory=lambda **kw: session) as sources:
            result = await sources.fetch("https://example.org/start")
        self.assertEqual(result["title"], "T")
        self.assertEqual(result["text"], "Full text")
        self.assertEqual(result["url"], "https://example.org/paper")
        self.assertTrue(all(call[1].get("allow_redirects") is False for call in session.calls))

    async def test_fetch_rejects_oversize_stream(self):
        session = FakeSession([FakeResponse(b"x" * (rs.MAX_DOWNLOAD_BYTES + 1), headers={"Content-Type": "text/plain"})])
        async with rs.PublicSources(session_factory=lambda **kw: session) as sources:
            with self.assertRaises(rs.SourceError):
                await sources.fetch("https://example.org/large")

    async def test_search_normalizes_deduplicates_and_keeps_partial_success(self):
        async def broken(query, limit):
            raise RuntimeError("provider unavailable")

        async def working(query, limit):
            return [
                {"title": " Paper ", "url": "https://example.org/p", "snippet": " abstract ", "extra": 1},
                {"title": "duplicate", "url": "https://example.org/p", "snippet": "x"},
            ]

        sources = rs.PublicSources(providers=[("broken", broken), ("papers", working)])
        results = await sources.search("topic", 5)
        self.assertEqual(results, [{"title": "Paper", "url": "https://example.org/p", "snippet": "abstract", "provider": "papers"}])

    async def test_search_raises_when_every_provider_fails(self):
        async def broken(query, limit):
            raise RuntimeError("nope")

        with self.assertRaises(rs.SourceError):
            await rs.PublicSources(providers=[("a", broken), ("b", broken)]).search("topic")

    async def test_search_validates_query_and_limit(self):
        sources = rs.PublicSources(providers=[])
        for query, limit in [("", 5), ("x", 0), ("x", 51)]:
            with self.subTest(query=query, limit=limit), self.assertRaises(SourceError := rs.SourceError):
                await sources.search(query, limit)


if __name__ == "__main__":
    unittest.main()
