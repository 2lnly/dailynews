"""Command line entry point: python -m dailynews"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .analyze import build_stories, collect_articles
from .fetch import fetch_all
from .render import render

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ("id", "name", "feed", "reliability", "bias", "topic", "note")
BIASES = ("left", "lean-left", "center", "lean-right", "right", "state")


def load_sources(path: Path) -> list[dict]:
    sources = json.loads(path.read_text(encoding="utf-8"))
    ids, ratings = set(), {}
    for src in sources:
        missing = [key for key in REQUIRED if key not in src]
        if missing:
            raise ValueError(f"source {src.get('id', '?')} is missing {missing}")
        if src["id"] in ids:
            raise ValueError(f"duplicate source id {src['id']}")
        ids.add(src["id"])
        if not isinstance(src["reliability"], int) or not 0 <= src["reliability"] <= 100:
            raise ValueError(f"{src['id']}: reliability must be an integer from 0 to 100")
        if src["bias"] not in BIASES:
            raise ValueError(f"{src['id']}: bias must be one of {BIASES}")
        if ratings.setdefault(src["name"], (src["reliability"], src["bias"])) != (src["reliability"], src["bias"]):
            raise ValueError(f"{src['name']}: feeds of one outlet must share a rating and bias")
    return sources


def outlet_ledger(sources: list[dict], results: list[dict], articles: list[dict]) -> list[dict]:
    """One row per outlet (an outlet may have several feeds)."""
    counts: dict[str, int] = {}
    for art in articles:
        counts[art["outlet"]] = counts.get(art["outlet"], 0) + 1
    rows: dict[str, dict] = {}
    for src, res in zip(sources, results):
        row = rows.setdefault(src["name"], {
            "name": src["name"], "reliability": src["reliability"], "bias": src["bias"],
            "note": src["note"], "articles": counts.get(src["name"], 0), "feeds": 0, "failed": 0, "error": "",
        })
        row["feeds"] += 1
        if not res["ok"]:
            row["failed"] += 1
            row["error"] = res["error"]
    return sorted(rows.values(), key=lambda r: (-r["reliability"], r["name"]))


def run(sources_path: Path, out_dir: Path, hours: float, timeout: float) -> dict:
    started = time.monotonic()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sources = load_sources(sources_path)
    results = fetch_all(sources, timeout=timeout)
    fetch_seconds = time.monotonic() - started
    articles = collect_articles(sources, results, now, window_hours=hours)
    stories = build_stories(articles, now)
    ledger = outlet_ledger(sources, results, articles)
    data = {
        "generated": now.isoformat(timespec="minutes"),
        "windowHours": hours,
        "stats": {
            "feeds": len(sources),
            "feedsOk": sum(1 for r in results if r["ok"]),
            "outlets": len(ledger),
            "articles": len(articles),
            "stories": len(stories),
            "multiSource": sum(1 for s in stories if s["outlets"] > 1),
            "fetchSeconds": round(fetch_seconds, 1),
            "totalSeconds": round(time.monotonic() - started, 1),
        },
        "stories": stories,
        "sources": ledger,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render(data), encoding="utf-8")
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dailynews", description="Scrape news feeds and build the scored report.")
    parser.add_argument("--sources", type=Path, default=ROOT / "sources.json", help="source list (default: sources.json)")
    parser.add_argument("--out", type=Path, default=ROOT / "site", help="output directory (default: site/)")
    parser.add_argument("--hours", type=float, default=36.0, help="how far back to look (default: 36)")
    parser.add_argument("--timeout", type=float, default=8.0, help="per-feed timeout in seconds (default: 8)")
    parser.add_argument("--min-feeds", type=float, default=0.5,
                        help="fail if fewer than this share of feeds could be read (default: 0.5)")
    args = parser.parse_args(argv)

    data = run(args.sources, args.out, args.hours, args.timeout)
    stats = data["stats"]
    print(f"{stats['feedsOk']}/{stats['feeds']} feeds, {stats['articles']} articles, {stats['stories']} stories "
          f"({stats['multiSource']} multi-source) in {stats['totalSeconds']}s -> {args.out / 'index.html'}")
    for row in data["sources"]:
        if row["failed"]:
            print(f"  feed failed: {row['name']}: {row['error']}", file=sys.stderr)
    if stats["feedsOk"] < args.min_feeds * stats["feeds"]:
        print("Too few feeds could be read; treating this run as failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
