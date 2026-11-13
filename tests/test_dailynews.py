import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dailynews.__main__ import load_sources, outlet_ledger
from dailynews.analyze import build_stories, classify_topic, collect_articles, reliability_label, tokenize
from dailynews.fetch import clean_text, parse_date, parse_feed, safe_url
from dailynews.render import render

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
<item><title>Storm &amp;amp; flood hit <b>coast</b></title><link>https://example.com/a?x=1</link>
<description><![CDATA[<p>Heavy   rain.</p>]]></description><pubDate>Fri, 13 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>No link here</title><pubDate>Fri, 13 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Bad scheme</title><link>javascript:alert(1)</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom story</title><link rel="self" href="https://example.com/self"/>
<link rel="alternate" href="https://example.com/atom"/><summary>Sum</summary>
<updated>2026-11-13T09:30:00Z</updated></entry></feed>"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>RDF story</title><link>https://example.com/rdf</link><dc:date>2026-11-13T08:00:00+01:00</dc:date></item>
</rdf:RDF>"""


UNRELATED = [
    "Central bank cuts interest rates for third time",
    "Local team wins regional chess tournament final",
    "Museum unveils restored medieval tapestry collection",
    "Airline cancels flights as pilots begin strike",
    "Researchers map genome of ancient wheat variety",
    "City council approves cycling lanes downtown",
    "Film festival awards top prize to debut director",
    "Farmers protest fertiliser prices outside parliament",
    "Volcano eruption forces evacuation of Icelandic village",
    "Carmaker recalls electric vehicles over battery fault",
    "Court overturns ban on rooftop solar subsidies",
    "Orchestra appoints youngest ever principal conductor",
]


def art(title, outlet, rating, hours_ago=1.0, bias="center", topic="general", summary=""):
    return {"title": title, "url": f"https://example.com/{outlet}/{abs(hash(title))}", "summary": summary,
            "published": NOW - timedelta(hours=hours_ago), "outlet": outlet, "reliability": rating,
            "bias": bias, "topic": topic, "prominence": 0.9}


class FetchTests(unittest.TestCase):
    def test_rss(self):
        items = parse_feed(RSS)
        self.assertEqual(len(items), 1)  # items without an http(s) link are dropped
        self.assertEqual(items[0]["title"], "Storm & flood hit coast")
        self.assertEqual(items[0]["summary"], "Heavy rain.")
        self.assertEqual(items[0]["published"], datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc))

    def test_atom_prefers_alternate_link(self):
        items = parse_feed(ATOM)
        self.assertEqual(items[0]["url"], "https://example.com/atom")
        self.assertEqual(items[0]["published"], datetime(2026, 11, 13, 9, 30, tzinfo=timezone.utc))

    def test_rdf_and_timezone_conversion(self):
        items = parse_feed(RDF)
        self.assertEqual(items[0]["published"], datetime(2026, 11, 13, 7, 0, tzinfo=timezone.utc))

    def test_helpers(self):
        self.assertIsNone(parse_date("not a date"))
        self.assertIsNone(parse_date(""))
        self.assertEqual(safe_url(" javascript:alert(1)"), "")
        self.assertEqual(safe_url("https://x.test/a"), "https://x.test/a")
        self.assertTrue(clean_text("word " * 100, 50).endswith("…"))
        self.assertLessEqual(len(clean_text("word " * 100, 50)), 51)


class AnalyzeTests(unittest.TestCase):
    def test_tokenize(self):
        self.assertEqual(tokenize("The Senate's new bills"), ["senate", "bill"])

    def test_collect_filters_window_and_duplicates(self):
        sources = [{"name": "A", "reliability": 80, "bias": "center", "topic": "general"}] * 2
        fresh = {"title": "Fresh story about things", "url": "https://a.test/1", "summary": "", "published": NOW - timedelta(hours=2)}
        items = [
            fresh,
            {"title": "Old story about things", "url": "https://a.test/2", "summary": "", "published": NOW - timedelta(hours=50)},
            {"title": "Undated story about things", "url": "https://a.test/3", "summary": "", "published": None},
            {"title": "Future story about things", "url": "https://a.test/4", "summary": "", "published": NOW + timedelta(days=2)},
            {"title": "Business", "url": "https://a.test/5", "summary": "", "published": NOW},
        ]
        results = [{"items": items}, {"items": [dict(fresh, url="https://a.test/1?utm=x")]}]
        got = collect_articles(sources, results, NOW)
        self.assertEqual([a["title"] for a in got], ["Fresh story about things"])

    def test_same_event_clusters_and_unrelated_does_not(self):
        articles = [
            art("Earthquake strikes northern Japan, tsunami warning issued", "A", 88),
            art("Tsunami warning after strong earthquake hits northern Japan", "B", 80),
            art("Japan earthquake: tsunami warning issued for northern coast", "C", 40),
        ] + [art(title, "D", 75) for title in UNRELATED]
        stories = build_stories(articles, NOW)
        self.assertEqual(len(stories), 1 + len(UNRELATED))
        top = stories[0]
        self.assertEqual(top["outlets"], 3)
        self.assertEqual(top["strongOutlets"], 2)
        # (88 + 80 + 40) / 3 = 69.3, plus 3 for one corroborating outlet rated 70+
        self.assertEqual(top["reliability"], 72)
        self.assertEqual(top["reliabilityLabel"], "Good")
        self.assertGreater(top["trend"], stories[1]["trend"])
        self.assertIn(top["articles"][0]["outlet"], "A")  # articles listed best-rated first
        self.assertNotEqual(top["title"], articles[2]["title"])  # headline never from the weak outlet

    def test_single_source_scores(self):
        story = build_stories([art("Mayor opens new bridge across river", "Tabloid", 30)], NOW)[0]
        self.assertEqual(story["reliability"], 30)
        self.assertEqual(story["flags"], ["Single source", "No reliable outlet"])
        self.assertLessEqual(story["trend"], 28)

    def test_one_sided_flag(self):
        articles = [art("Governor signs sweeping housing reform bill today", o, 70, bias=b)
                    for o, b in (("A", "left"), ("B", "lean-left"), ("C", "left"))]
        story = build_stories(articles, NOW)[0]
        self.assertEqual(story["outlets"], 3)
        self.assertIn("Only left-leaning outlets", story["flags"])
        self.assertEqual(story["reliability"], 76)  # 70 average + 2 corroborating outlets * 3

    def test_scores_stay_in_range(self):
        articles = [art("Global summit ends with landmark climate agreement signed", f"O{i}", 95) for i in range(12)]
        story = build_stories(articles, NOW)[0]
        self.assertEqual(story["reliability"], 99)
        self.assertTrue(0 <= story["trend"] <= 100)

    def test_labels_and_topics(self):
        self.assertEqual([reliability_label(x) for x in (80, 79, 65, 64, 45, 44)],
                         ["High", "Good", "Good", "Mixed", "Mixed", "Low"])
        self.assertEqual(classify_topic([art("Senate passes election bill after vote", "A", 80)]), "politics")
        self.assertEqual(classify_topic([art("Ferry sinks off island coast", "A", 80)]), "world")


class OutputTests(unittest.TestCase):
    def test_sources_file_is_valid(self):
        sources = load_sources(ROOT / "sources.json")
        self.assertGreater(len(sources), 50)
        self.assertTrue(all(s["feed"].startswith("https://") for s in sources))

    def test_ledger_merges_feeds_per_outlet(self):
        sources = [{"name": "A", "reliability": 80, "bias": "center", "note": "n"}] * 2
        results = [{"ok": True, "error": ""}, {"ok": False, "error": "HTTP 500"}]
        rows = outlet_ledger(sources, results, [{"outlet": "A"}])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["feeds"], rows[0]["failed"], rows[0]["articles"]), (2, 1, 1))

    def test_render_escapes_embedded_data(self):
        data = {"generated": "2026-11-13T12:00+00:00", "windowHours": 36, "stats": {}, "sources": [],
                "stories": [{"title": "</script><script>alert(1)</script>"}]}
        page = render(data)
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertNotIn("</script><script>alert(1)", page)
        start = page.index('id="data">') + len('id="data">')
        embedded = page[start:page.index("</script>", start)]
        self.assertEqual(json.loads(embedded), data)
        self.assertNotIn("<!doctype", render(data, fragment=True))


if __name__ == "__main__":
    unittest.main()
