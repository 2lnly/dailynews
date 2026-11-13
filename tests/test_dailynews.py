import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dailynews.__main__ import load_sources, outlet_ledger
from dailynews.__main__ import attention_list
from dailynews.analyze import build_stories as _build_stories
from dailynews.analyze import classify_topic, collect_articles, reliability_label
from dailynews.fetch import clean_text, parse_date, parse_feed, safe_url
from dailynews.narrative import _names_in, find_entities, framing, sentences, typical_account
from dailynews.render import render
from dailynews.social import _traffic, signal
from dailynews.text import tokenize

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


GOOGLE = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Storm hits coast - Example Wire</title><link>https://news.google.com/rss/articles/abc?oc=5</link>
<description>&lt;a href="x"&gt;Storm hits coast&lt;/a&gt; Example Wire</description>
<pubDate>Fri, 13 Nov 2026 10:00:00 GMT</pubDate><source url="https://example.com">Example Wire</source></item>
</channel></rss>"""


def build_stories(articles, now, signals=None):
    return _build_stories(articles, now, signals)[0]


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

    def test_google_news_items_lose_publisher_suffix_and_fake_summary(self):
        item = parse_feed(GOOGLE)[0]
        self.assertEqual(item["title"], "Storm hits coast")
        self.assertEqual(item["summary"], "")

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
        self.assertLessEqual(story["trend"], 22)  # no coverage and no social attention
        self.assertEqual((story["account"], story["framing"], story["entities"]), ([], [], []))

    def test_wire_copy_is_not_counted_as_confirmation(self):
        same = "Regulator fines airline over cancelled holiday flights"
        articles = [art(same, "Wire", 90, hours_ago=3), art(same, "Reprint A", 80, hours_ago=2),
                    art(same, "Reprint B", 80, hours_ago=1)] + [art(t, "D", 75) for t in UNRELATED]
        story = build_stories(articles, NOW)[0]
        self.assertEqual((story["outlets"], story["copies"], story["strongOutlets"]), (3, 2, 1))
        self.assertEqual(story["reliability"], 83)  # (90 + 80 + 80) / 3 and no corroboration bonus

    def test_social_signal_raises_trend_not_reliability(self):
        articles = [art("Earthquake strikes northern Japan, tsunami warning issued", "A", 88),
                    art("Tsunami warning after strong earthquake hits northern Japan", "B", 80)]
        articles += [art(t, "D", 75) for t in UNRELATED]
        plain = build_stories([dict(a) for a in articles], NOW)[0]
        signals = [signal("Reddit", "Strong earthquake hits northern Japan, tsunami warning issued", "https://r.test/1", 9, "#1"),
                   signal("Reddit", "My cat learned to open the fridge", "https://r.test/2", 5, "#2")]
        stories, unmatched = _build_stories([dict(a) for a in articles], NOW, signals)
        self.assertEqual([g["platform"] for g in stories[0]["social"]], ["Reddit"])
        self.assertGreater(stories[0]["trend"], plain["trend"])
        self.assertEqual(stories[0]["reliability"], plain["reliability"])
        self.assertEqual([u["text"] for u in unmatched], ["My cat learned to open the fridge"])
        self.assertEqual([r["url"] for r in attention_list(unmatched)], ["https://r.test/2"])

    def test_one_sided_flag(self):
        articles = [art(f"Governor signs sweeping housing reform bill {word}", o, 70, bias=b)
                    for o, b, word in (("A", "left", "today"), ("B", "lean-left", "quietly"), ("C", "left", "finally"))]
        articles += [art(t, "D", 75) for t in UNRELATED]  # background, so shared words carry weight
        story = build_stories(articles, NOW)[0]
        self.assertEqual(story["outlets"], 3)
        self.assertIn("Only left-leaning outlets", story["flags"])
        self.assertEqual(story["reliability"], 76)  # 70 average + 2 corroborating outlets * 3

    def test_scores_stay_in_range(self):
        extra = "paris delegates cheer negotiators overnight".split()
        articles = [art(f"Global summit ends with landmark climate agreement signed {extra[i]}", f"O{i}", 95)
                    for i in range(5)] + [art(t, "D", 75) for t in UNRELATED]
        story = build_stories(articles, NOW)[0]
        self.assertEqual(story["outlets"], 5)
        self.assertEqual(story["reliability"], 99)  # 95 + 9 would be 104: capped
        self.assertTrue(0 <= story["trend"] <= 100)

    def test_labels_and_topics(self):
        self.assertEqual([reliability_label(x) for x in (80, 79, 65, 64, 45, 44)],
                         ["High", "Good", "Good", "Mixed", "Mixed", "Low"])
        self.assertEqual(classify_topic([art("Senate passes election bill after vote", "A", 80)]), "politics")
        self.assertEqual(classify_topic([art("Ferry sinks off island coast", "A", 80)]), "world")


class NarrativeTests(unittest.TestCase):
    def test_sentence_splitting_keeps_abbreviations_and_drops_datelines(self):
        self.assertEqual(sentences("LIMA, Nov 12 - Crowds filled the streets. Sen. Lee of the U.S. Senate spoke. Done."),
                         ["Crowds filled the streets.", "Sen. Lee of the U.S. Senate spoke.", "Done."])
        self.assertEqual(sentences("A well-known man - a hero - died."), ["A well-known man - a hero - died."])

    def test_names(self):
        self.assertEqual(_names_in("Nvidia CEO Jensen Huang met Russian President Vladimir Putin's envoys in New York City."),
                         ["Jensen Huang", "Vladimir Putin", "New York City"])
        self.assertEqual(_names_in("Gunman flees the Tomb of the Unknown Soldier, NATO says on Monday."),
                         ["Tomb of the Unknown Soldier", "NATO"])
        self.assertEqual(_names_in("A crash in the United Arab Emirates’ Fujairah emirate after an Israeli strike."),
                         ["United Arab Emirates", "Fujairah"])

    def test_entities_fold_short_forms_and_need_two_outlets(self):
        members = [art("Jensen Huang resigns as chief of Nvidia", "A", 80, summary="The board accepted it. Huang led Nvidia for decades."),
                   art("Board ousts Jensen Huang in spy scandal", "B", 80, summary="Shares in Nvidia fell."),
                   art("Why Huang had to go", "C", 80, summary="Analysts in Taipei reacted.")]
        self.assertEqual(find_entities(members), [{"name": "Jensen Huang", "outlets": 3}, {"name": "Nvidia", "outlets": 2}])

    def test_typical_account_quotes_the_most_shared_sentence(self):
        members = [
            art("Dam bursts in valley", "A", 85, summary="A dam burst in the northern valley on Tuesday, flooding three villages and forcing thousands to flee."),
            art("Thousands flee as dam bursts", "B", 80, summary="Our reporter recalls a childhood spent fishing below the old wall. Read more in the newsletter."),
            art("Dam burst floods villages in northern valley", "C", 30, summary="Shocking dam burst floods villages in northern valley as thousands flee in terror, say locals."),
        ]
        account = typical_account(members)
        self.assertEqual(account[0]["outlet"], "A")  # most shared wording, and never from the low-rated outlet
        self.assertTrue(account[0]["text"].startswith("A dam burst in the northern valley"))
        self.assertNotIn("C", [a["outlet"] for a in account])
        self.assertFalse(account[0]["headline"])

    def test_account_falls_back_to_headline_without_summaries(self):
        members = [art("Dam bursts in northern valley", "A", 85), art("Dam bursts, villages flooded in valley", "B", 80)]
        account = typical_account(members)
        self.assertEqual(len(account), 1)
        self.assertTrue(account[0]["headline"])

    def test_framing_gives_one_headline_per_side(self):
        members = [art("Tax bill passes senate", "A", 80, bias="lean-left"), art("Senate passes tax bill", "B", 80, bias="right"),
                   art("Senate approves tax bill", "C", 80, bias="right")]
        got = framing(members)
        self.assertEqual([(f["side"], f["outlets"]) for f in got], [("left", 1), ("right", 2)])
        self.assertEqual(framing(members[1:]), [])  # one side only: nothing to compare

    def test_traffic_parsing(self):
        self.assertEqual([_traffic(v) for v in ("200+", "2K+", "1.5M+", "", "n/a")], [200, 2000, 1500000, 0, 0])


class OutputTests(unittest.TestCase):
    def test_sources_file_is_valid(self):
        sources = load_sources(ROOT / "sources.json")
        self.assertGreater(len(sources), 150)
        self.assertTrue(all(s["feed"].startswith("https://") for s in sources))

    def test_ledger_merges_feeds_per_outlet(self):
        sources = [{"name": "A", "reliability": 80, "bias": "center", "note": "n"}] * 2
        results = [{"ok": True, "error": ""}, {"ok": False, "error": "HTTP 500"}]
        rows = outlet_ledger(sources, results, [{"outlet": "A"}])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["feeds"], rows[0]["failed"], rows[0]["articles"]), (2, 1, 1))

    def test_render_escapes_embedded_data(self):
        data = {"generated": "2026-11-13T12:00+00:00", "windowHours": 36, "stats": {}, "sources": [],
                "attention": [], "platforms": [],
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
