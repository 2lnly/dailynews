"""Concurrent RSS/Atom fetching and parsing. Standard library only."""
from __future__ import annotations

import gzip
import html
import json
import re
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

USER_AGENT = "Mozilla/5.0 (compatible; dailynews-scraper/1.0)"
MAX_BYTES = 12 * 1024 * 1024
TAG_RE = re.compile(r"<[^>]*>")
WS_RE = re.compile(r"\s+")
ZERO_WIDTH_RE = re.compile("[\u200b-\u200d\u2060\ufeff]")
# Characters that are illegal in XML 1.0 but turn up in real feeds.
BAD_XML_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _local(tag: str) -> str:
    """Tag name without its XML namespace."""
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def clean_text(value: str | None, limit: int = 0) -> str:
    """Strip markup and collapse whitespace; optionally truncate on a word boundary."""
    if not value:
        return ""
    text = html.unescape(TAG_RE.sub(" ", html.unescape(value)))
    text = ZERO_WIDTH_RE.sub("", text)
    text = WS_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return text


def parse_date(value: str | None) -> datetime | None:
    """Parse RFC 822 or ISO 8601 dates into aware UTC datetimes."""
    if not value:
        return None
    value = value.strip()
    parsed = None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_url(value: str | None) -> str:
    value = (value or "").strip()
    return value if value.lower().startswith(("http://", "https://")) else ""


def parse_feed(data: bytes) -> list[dict]:
    """Return [{title, url, summary, published}] in feed order for RSS 2.0, RDF and Atom."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        root = ET.fromstring(BAD_XML_RE.sub(b"", data))
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = url = summary = origin = ""
        published = None
        for child in node:
            name = _local(child.tag)
            text = "".join(child.itertext())  # also covers unescaped inline markup
            if name == "title" and not title:
                title = clean_text(text)
            elif name == "link":
                href = child.get("href")
                if href:  # Atom: prefer the rel="alternate" (or rel-less) link
                    if child.get("rel", "alternate") == "alternate" or not url:
                        url = safe_url(href) or url
                elif not url:
                    url = safe_url(text)
            elif name in ("description", "summary") and not summary:
                summary = clean_text(text, 300)
            elif name == "source":
                origin = clean_text(text)
            elif name in ("pubdate", "published", "date") or (name == "updated" and published is None):
                published = parse_date(text) or published
        if "//news.google.com/" in url:
            # Google News appends " - Publisher" to headlines and has no real summary.
            if origin and title.endswith(" - " + origin):
                title = title[: -len(origin) - 3].rstrip()
            summary = ""
        if title and url:
            items.append({"title": title, "url": url, "summary": summary, "published": published})
    return items


_host_locks: dict[str, threading.Semaphore] = {}
_host_locks_guard = threading.Lock()


def _host_slot(url: str) -> threading.Semaphore:
    """At most 6 simultaneous requests per host, so no single site is hammered."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    with _host_locks_guard:
        return _host_locks.setdefault(host, threading.Semaphore(6))


def _download(url: str, timeout: float, accept: str = "") -> bytes:
    with _host_slot(url):
        return _download_now(url, timeout, accept)


def fetch_json(url: str, timeout: float = 8.0):
    """GET a JSON API. Raises on any failure; callers decide how to degrade."""
    return json.loads(_download(url, timeout, "application/json").decode("utf-8", "replace"))


def _download_now(url: str, timeout: float, accept: str = "") -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept or "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
        encoding = (response.headers.get("Content-Encoding") or "").lower()
    if len(body) > MAX_BYTES:
        raise ValueError("feed larger than 12 MB")
    if encoding == "gzip" or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    elif encoding == "deflate":
        try:
            body = zlib.decompress(body)
        except zlib.error:
            body = zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def fetch_source(source: dict, timeout: float = 8.0, retries: int = 1) -> dict:
    """Fetch one source, retrying once on transient errors. Never raises: failures are reported."""
    started = time.monotonic()
    result = {"id": source["id"], "ok": False, "error": "", "items": [], "seconds": 0.0}
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(0.6 * attempt)
        try:
            result["items"] = parse_feed(_download(source["feed"], timeout))
            result["ok"] = bool(result["items"])
            result["error"] = "" if result["ok"] else "feed had no readable items"
            break
        except urllib.error.HTTPError as exc:
            result["error"] = f"HTTP {exc.code}"
            if exc.code in (400, 401, 404, 410):
                break  # retrying will not help
        except Exception as exc:  # network, TLS, timeout, malformed XML
            result["error"] = f"{type(exc).__name__}: {exc}"[:160]
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def fetch_all(sources: list[dict], workers: int = 48, timeout: float = 8.0) -> list[dict]:
    """Fetch every source in parallel, preserving input order."""
    if not sources:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(sources))) as pool:
        return list(pool.map(lambda s: fetch_source(s, timeout), sources))
