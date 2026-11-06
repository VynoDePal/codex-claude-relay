"""Bounded, read-only access to public documentary research sources."""

from __future__ import annotations

import asyncio
import html
from html.parser import HTMLParser
import ipaddress
import json
import os
import shutil
import socket
import tempfile
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver


MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 30_000
MAX_REDIRECTS = 4
FETCH_TIMEOUT_SECONDS = 20
_USER_AGENT = "UltraN-DocumentaryResearch/1.0 (public read-only research)"


class SourceError(Exception):
    """A public source could not be searched or safely fetched."""


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return address.is_global and not address.is_multicast
    except ValueError:
        return False


def _validate_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise SourceError("URL must be a non-empty string")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise SourceError("Malformed URL") from exc
    if parsed.scheme not in {"http", "https"}:
        raise SourceError("Only HTTP(S) sources are supported")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise SourceError("URL host is missing or contains credentials")
    if port is not None and not (1 <= port <= 65535):
        raise SourceError("Invalid URL port")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise SourceError("Local source URLs are not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not _public_ip(str(address)):
            raise SourceError("Non-public source address is not allowed")
    return url


class SafeResolver(AbstractResolver):
    """Resolve a host and reject the whole answer if any address is non-public."""

    def __init__(self, getaddrinfo: Callable[..., Awaitable[list]] | None = None):
        self._getaddrinfo = getaddrinfo

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        if self._getaddrinfo is None:
            loop = asyncio.get_running_loop()
            answers = await loop.getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
        else:
            answers = await self._getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
        if not answers:
            raise OSError("Host did not resolve")
        resolved = []
        for answer_family, _, proto, _, sockaddr in answers:
            address = sockaddr[0]
            if not _public_ip(address):
                raise OSError("Host resolved to a non-public address")
            resolved.append(
                {"hostname": host, "host": address, "port": port,
                 "family": answer_family, "proto": proto, "flags": 0}
            )
        return resolved

    async def close(self) -> None:
        return None


class _ReadableHTML(HTMLParser):
    _skip_tags = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form"}
    _break_tags = {"p", "div", "main", "article", "section", "br", "li", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._skip_tags:
            self.skip += 1
        elif not self.skip:
            if tag == "title":
                self.in_title = True
            if tag in self._break_tags:
                self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._skip_tags:
            self.skip = max(0, self.skip - 1)
        elif not self.skip:
            if tag == "title":
                self.in_title = False
            if tag in self._break_tags:
                self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title_parts.append(data)
        else:
            self.parts.append(data)


def _clean_text(value: str) -> str:
    lines = (" ".join(line.split()) for line in value.replace("\r", "\n").split("\n"))
    return "\n".join(line for line in lines if line).strip()[:MAX_TEXT_CHARS]


def _extract_html(body: bytes, charset: str | None = None) -> tuple[str, str]:
    parser = _ReadableHTML()
    try:
        parser.feed(body.decode(charset or "utf-8", errors="replace"))
    except (LookupError, UnicodeError) as exc:
        raise SourceError("Could not decode HTML source") from exc
    return _clean_text(" ".join(parser.title_parts)), _clean_text("".join(parser.parts))


async def _pdf_text(body: bytes) -> str:
    executable = shutil.which("pdftotext")
    if not executable:
        raise SourceError("PDF source found, but no safe local PDF parser is available")
    with tempfile.TemporaryDirectory(prefix="ultra-n-pdf-") as directory:
        source = os.path.join(directory, "source.pdf")
        with open(source, "wb") as handle:
            handle.write(body)
        process = await asyncio.create_subprocess_exec(
            executable, "-enc", "UTF-8", source, "-",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        pieces: list[str] = []
        chars = 0
        truncated = False
        code = None
        try:
            async with asyncio.timeout(10):
                while True:
                    chunk = await process.stdout.read(8192)
                    if not chunk:
                        break
                    decoded = chunk.decode("utf-8", errors="replace")
                    remaining = MAX_TEXT_CHARS - chars
                    pieces.append(decoded[:remaining])
                    chars += min(len(decoded), remaining)
                    if chars >= MAX_TEXT_CHARS:
                        truncated = True
                        break
                if not truncated:
                    code = await process.wait()
        except asyncio.TimeoutError as exc:
            raise SourceError("PDF parsing timed out") from exc
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # Once killed, drain only the pipe buffer while discarding it. This
            # lets transports close and process.wait() reap without retaining
            # expanded PDF output in memory or on disk.
            while await process.stdout.read(8192):
                pass
            final_code = await process.wait()
            if code is None:
                code = final_code
        if code and not truncated:
            raise SourceError("Could not parse PDF source")
        return _clean_text("".join(pieces))


Provider = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


class PublicSources:
    def __init__(self, *, session_factory=None, providers: list[tuple[str, Provider]] | None = None,
                 resolver: AbstractResolver | None = None):
        self._session_factory = session_factory or aiohttp.ClientSession
        self._resolver = resolver or SafeResolver()
        self._session = None
        self._providers = providers

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _ensure_session(self):
        if self._session is None:
            connector = aiohttp.TCPConnector(resolver=self._resolver, use_dns_cache=False)
            self._session = self._session_factory(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
            )
        return self._session

    async def _read_bounded(self, response) -> bytes:
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > MAX_DOWNLOAD_BYTES:
                    raise SourceError("Source exceeds the 5 MB download limit")
            except ValueError:
                pass
        chunks, total = [], 0
        stream = getattr(response, "content", response)
        while True:
            chunk = await stream.read(min(64 * 1024, MAX_DOWNLOAD_BYTES + 1 - total))
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise SourceError("Source exceeds the 5 MB download limit")
            chunks.append(chunk)

    async def fetch(self, url: str) -> dict:
        current = _validate_url(url)
        session = await self._ensure_session()
        try:
            async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
                for redirects in range(MAX_REDIRECTS + 1):
                    response = await session.get(current, allow_redirects=False)
                    try:
                        if response.status in {301, 302, 303, 307, 308}:
                            if redirects == MAX_REDIRECTS:
                                raise SourceError("Source exceeded the redirect limit")
                            location = response.headers.get("Location")
                            if not location:
                                raise SourceError("Source returned a redirect without a location")
                            current = _validate_url(urljoin(current, location))
                            continue
                        if response.status < 200 or response.status >= 300:
                            raise SourceError(f"Source returned HTTP {response.status}")
                        body = await self._read_bounded(response)
                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                        charset = None
                        if hasattr(response, "charset"):
                            charset = response.charset
                        final_url = str(getattr(response, "url", current))
                        if content_type == "application/pdf" or body.startswith(b"%PDF-"):
                            text = await _pdf_text(body)
                            title = final_url.rsplit("/", 1)[-1]
                            content_type = "application/pdf"
                        elif content_type in {"text/html", "application/xhtml+xml"} or b"<html" in body[:1024].lower():
                            title, text = _extract_html(body, charset)
                            content_type = content_type or "text/html"
                        elif content_type.startswith("text/") or content_type in {"application/json", "application/xml"}:
                            try:
                                text = _clean_text(body.decode(charset or "utf-8", errors="replace"))
                            except LookupError as exc:
                                raise SourceError("Could not decode text source") from exc
                            title = final_url.rsplit("/", 1)[-1]
                        else:
                            raise SourceError("Unsupported source content type")
                        if not text:
                            raise SourceError("Source contained no readable text")
                        return {"url": final_url, "title": title, "text": text, "content_type": content_type}
                    finally:
                        response.release()
        except SourceError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise SourceError("Public source fetch failed") from exc

    async def _hf_search(self, query: str, limit: int) -> list[dict]:
        session = await self._ensure_session()
        url = f"https://huggingface.co/api/papers/search?q={quote(query)}&limit={limit}"
        response = await session.get(_validate_url(url), allow_redirects=False)
        try:
            if response.status != 200:
                raise SourceError(f"Hugging Face Papers returned HTTP {response.status}")
            payload = json.loads((await self._read_bounded(response)).decode("utf-8"))
        finally:
            response.release()
        items = payload if isinstance(payload, list) else payload.get("papers", [])
        results = []
        for item in items:
            nested = item.get("paper") if isinstance(item.get("paper"), dict) else {}
            paper_id = str(item.get("paperId") or item.get("id") or nested.get("id") or "").strip()
            title = str(item.get("title") or nested.get("title") or "").strip()
            if paper_id and title:
                summary = str(item.get("summary") or item.get("abstract") or nested.get("summary") or "").strip()
                results.append({"title": title, "url": f"https://huggingface.co/papers/{paper_id}",
                                "snippet": f"Abstract/provider metadata only. {summary}".strip()})
        return results

    async def _crossref_search(self, query: str, limit: int) -> list[dict]:
        session = await self._ensure_session()
        url = f"https://api.crossref.org/works?query={quote(query)}&rows={limit}&select=DOI,title,abstract,URL"
        response = await session.get(_validate_url(url), allow_redirects=False)
        try:
            if response.status != 200:
                raise SourceError(f"Crossref returned HTTP {response.status}")
            payload = json.loads((await self._read_bounded(response)).decode("utf-8"))
        finally:
            response.release()
        results = []
        for item in payload.get("message", {}).get("items", []):
            titles = item.get("title") or []
            title = str(titles[0] if titles else "").strip()
            target = str(item.get("URL") or "").strip()
            if title and target:
                abstract = _clean_text(html.unescape(_extract_html(str(item.get("abstract") or "").encode(), "utf-8")[1]))
                results.append({"title": title, "url": target,
                                "snippet": f"Abstract/provider metadata only. {abstract}".strip()})
        return results

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise SourceError("Search query must be non-empty")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise SourceError("Search limit must be between 1 and 50")
        providers = self._providers
        if providers is None:
            providers = [("huggingface-papers", self._hf_search), ("crossref", self._crossref_search)]
        outcomes = await asyncio.gather(
            *(provider(query.strip(), limit) for _, provider in providers), return_exceptions=True
        )
        normalized, seen, succeeded = [], set(), 0
        for (provider_name, _), outcome in zip(providers, outcomes):
            if isinstance(outcome, BaseException):
                continue
            succeeded += 1
            for item in outcome:
                try:
                    title = str(item["title"]).strip()
                    target = _validate_url(str(item["url"]).strip())
                    snippet = _clean_text(str(item.get("snippet") or ""))
                except (KeyError, SourceError):
                    continue
                if not title or target in seen:
                    continue
                seen.add(target)
                normalized.append({"title": title, "url": target, "snippet": snippet,
                                   "provider": provider_name})
                if len(normalized) >= limit:
                    return normalized
        if not succeeded:
            raise SourceError("All public search providers failed")
        return normalized


__all__ = ["SourceError", "PublicSources"]
