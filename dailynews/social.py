"""Attention signals from social platforms and public trend data.

Each platform returns "signals": things people are sharing or looking up right now. They are
matched to news stories to measure buzz; they are never treated as reporting.
X (Twitter) is not here: it has no free or anonymous read access.
"""
from __future__ import annotations

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from .fetch import _download, _local, clean_text, fetch_json, parse_feed, safe_url

REDDIT_SUBS = "worldnews+news+politics+technology+science+business+geopolitics"
WIKI_SKIP = re.compile(r"^(Main_Page$|Special:|Wikipedia:|Portal:|File:|Help:|Category:|Template:|Talk:|User:|\.)|"
                       r"^(Deaths_in_\d{4}|\d{4}|Cookie_\(.*|XXX.*|Pornhub|YouTube|Google|Facebook|ChatGPT|Bible)$")


def signal(platform: str, text: str, url: str, score: float, metric: str, *, kind: str = "text",
           texts: list[str] | None = None, listable: bool = True) -> dict:
    return {"platform": platform, "text": clean_text(text, 200), "url": safe_url(url), "score": float(score),
            "metric": metric, "kind": kind, "texts": texts or [clean_text(text, 300)], "listable": listable}


def hacker_news(now: datetime, hours: float, timeout: float) -> list[dict]:
    since = int((now - timedelta(hours=hours)).timestamp())
    url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=100&numericFilters="
           + urllib.parse.quote(f"created_at_i>{since},points>100"))
    out = []
    for hit in fetch_json(url, timeout)["hits"]:
        if not hit.get("title"):
            continue
        points, comments = int(hit.get("points") or 0), int(hit.get("num_comments") or 0)
        out.append(signal("Hacker News", hit["title"], f"https://news.ycombinator.com/item?id={hit['objectID']}",
                          points + comments, f"{points:,} points, {comments:,} comments"))
    return out


def reddit(now: datetime, hours: float, timeout: float) -> list[dict]:
    feed = parse_feed(_download(f"https://www.reddit.com/r/{REDDIT_SUBS}/top/.rss?t=day&limit=100", timeout))
    total = len(feed)
    return [signal("Reddit", item["title"], item["url"], total - rank, f"#{rank + 1} of today's top news posts")
            for rank, item in enumerate(feed)]


def mastodon(now: datetime, hours: float, timeout: float) -> list[dict]:
    out = []
    for offset in (0, 20):
        for link in fetch_json(f"https://mastodon.social/api/v1/trends/links?limit=20&offset={offset}", timeout):
            accounts = sum(int(day.get("accounts") or 0) for day in (link.get("history") or [])[:2])
            if link.get("title"):
                out.append(signal("Mastodon", link["title"], link.get("url", ""), accounts,
                                  f"shared by {accounts:,} accounts in two days",
                                  texts=[clean_text(link["title"], 300), clean_text(link.get("description", ""), 300)]))
    return out


def bluesky(now: datetime, hours: float, timeout: float) -> list[dict]:
    api = "https://public.api.bsky.app/xrpc/"
    out = []
    topics = fetch_json(api + "app.bsky.unspecced.getTrendingTopics?limit=25", timeout).get("topics", [])
    for rank, topic in enumerate(topics):
        name = topic.get("displayName") or topic.get("topic") or ""
        if name:
            out.append(signal("Bluesky", name, "https://bsky.app" + topic.get("link", ""), 10_000_000 - rank,
                              f"trending topic #{rank + 1}",
                              texts=[clean_text(name + ". " + (topic.get("description") or ""), 300)]))
    hot = "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot"
    feed = fetch_json(api + "app.bsky.feed.getFeed?limit=100&feed=" + urllib.parse.quote(hot, safe=""), timeout)
    for entry in feed.get("feed", []):
        post = entry.get("post") or {}
        record = post.get("record") or {}
        langs = record.get("langs") or []
        text = record.get("text") or ""
        external = (post.get("embed") or {}).get("external") or {}
        if (langs and "en" not in langs) or len(text.split()) + len((external.get("title") or "").split()) < 6:
            continue
        likes, reposts = int(post.get("likeCount") or 0), int(post.get("repostCount") or 0)
        handle = (post.get("author") or {}).get("handle", "")
        rkey = (post.get("uri") or "").rsplit("/", 1)[-1]
        out.append(signal("Bluesky", text, f"https://bsky.app/profile/{handle}/post/{rkey}", likes + 2 * reposts,
                          f"post with {likes:,} likes, {reposts:,} reposts",
                          texts=[clean_text(text + " " + (external.get("title") or ""), 300)], listable=False))
    return out


def _traffic(value: str) -> int:
    match = re.match(r"\s*([\d.,]+)\s*([KMB]?)", value or "", re.I)
    if not match:
        return 0
    number = float(match.group(1).replace(",", ""))
    return int(number * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[match.group(2).upper()])


def google_trends(now: datetime, hours: float, timeout: float) -> list[dict]:
    root = ET.fromstring(_download("https://trends.google.com/trending/rss?geo=US", timeout))
    out = []
    for item in root.iter("item"):
        query, traffic, headlines, link = "", "", [], ""
        for child in item:
            name = _local(child.tag)
            if name == "title":
                query = clean_text(child.text)
            elif name == "approx_traffic":
                traffic = (child.text or "").strip()
            elif name == "news_item":
                for sub in child:
                    if _local(sub.tag) == "news_item_title" and sub.text:
                        headlines.append(clean_text(sub.text, 300))
                    elif _local(sub.tag) == "news_item_url" and not link:
                        link = safe_url(sub.text)
        if query and headlines:
            out.append(signal("Google Trends", query, "https://trends.google.com/trending?geo=US",
                              _traffic(traffic), f"{traffic} US searches" if traffic else "trending US search",
                              texts=headlines[:3]))
    return out


def lemmy(now: datetime, hours: float, timeout: float) -> list[dict]:
    out = []
    for community in ("world@lemmy.world", "news@lemmy.world"):
        data = fetch_json("https://lemmy.world/api/v3/post/list?sort=TopDay&limit=50&community_name="
                          + urllib.parse.quote(community), timeout)
        for entry in data.get("posts", []):
            post, counts = entry.get("post") or {}, entry.get("counts") or {}
            score, comments = int(counts.get("score") or 0), int(counts.get("comments") or 0)
            if post.get("name"):
                out.append(signal("Lemmy", post["name"], post.get("ap_id", ""), score + comments,
                                  f"{score:,} points, {comments:,} comments"))
    return out


def wikipedia(now: datetime, hours: float, timeout: float) -> list[dict]:
    """Most-read English Wikipedia articles on the latest complete day; matched by name, not wording."""
    last_error: Exception | None = None
    for days_back in (1, 2):
        day = now - timedelta(days=days_back)
        url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{day:%Y/%m/%d}"
        try:
            articles = fetch_json(url, timeout)["items"][0]["articles"]
        except Exception as exc:  # the newest day is sometimes not published yet
            last_error = exc
            continue
        out = []
        for art in articles:
            title = art.get("article", "")
            if WIKI_SKIP.search(title) or len(out) >= 300:
                continue
            name = re.sub(r"_\(.*\)$", "", title).replace("_", " ")
            out.append(signal("Wikipedia", name, "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title),
                              art.get("views", 0), f"{int(art.get('views', 0)):,} page views on {day:%b} {day.day}",
                              kind="entity", listable=False))
        return out
    raise last_error or RuntimeError("no data")


PLATFORMS = [("Hacker News", hacker_news), ("Reddit", reddit), ("Mastodon", mastodon), ("Bluesky", bluesky),
             ("Google Trends", google_trends), ("Lemmy", lemmy), ("Wikipedia", wikipedia)]


def fetch_signals(now: datetime, hours: float = 36.0, timeout: float = 8.0) -> tuple[list[dict], list[dict]]:
    """Query every platform in parallel. Returns (signals, per-platform status). Never raises."""

    def run(entry):
        name, func = entry
        started = time.monotonic()
        status = {"platform": name, "ok": False, "items": 0, "error": ""}
        items: list[dict] = []
        for attempt in range(2):
            try:
                items = [s for s in func(now, hours, timeout) if s["text"]]
                status.update(ok=bool(items), items=len(items), error="" if items else "returned nothing")
                break
            except Exception as exc:
                status["error"] = f"{type(exc).__name__}: {exc}"[:160]
                time.sleep(0.5)
        status["seconds"] = round(time.monotonic() - started, 2)
        return items, status

    with ThreadPoolExecutor(max_workers=len(PLATFORMS)) as pool:
        results = list(pool.map(run, PLATFORMS))
    return [s for items, _ in results for s in items], [status for _, status in results]
