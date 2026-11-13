"""Turn fetched articles into ranked stories: dedupe, cluster, then score trend and reliability."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .narrative import BIAS_SIDES, find_entities, framing, typical_account
from .text import tokenize

# Clustering thresholds, tuned by eye on real feeds: lower merges more, higher splits more.
PAIR_THRESHOLD = 0.34      # two articles may be linked at or above this similarity
CLUSTER_THRESHOLD = 0.30   # the two groups being merged must also be this similar as wholes
MAX_SPAN = timedelta(hours=48)

SOCIAL_THRESHOLD = 0.40    # a social post must be this similar to an article to count as buzz for it
# Headlines that label a format rather than state the news; avoided as a story's title.
LABELLED_RE = re.compile(r"(?i)^(watch|live|video|listen|exclusive|explainer|analysis|opinion|factbox|podcast"
                         r"|news wrap|what (we|to) know|\d+ (things|takeaways|facts))\b")
COMMON_TOKEN = 400         # tokens in more articles than this are too common to discriminate
# Wikipedia pages that are heavily read every day whatever the news is.
GENERIC_PAGES = frozenset((
    "united states", "us", "china", "india", "russia", "ukraine", "israel", "iran", "united kingdom", "uk",
    "europe", "donald trump", "elon musk", "japan", "germany", "france", "canada", "australia"))


def collect_articles(sources: list[dict], results: list[dict], now: datetime,
                     window_hours: float = 36.0, per_feed: int = 60) -> list[dict]:
    """Flatten feed results into fresh, de-duplicated articles carrying their outlet's metadata."""
    earliest = now - timedelta(hours=window_hours)
    latest = now + timedelta(hours=1)  # tolerate small clock skew, drop far-future dates
    seen_urls, seen_titles, articles = set(), set(), []
    for source, result in zip(sources, results):
        fresh = [(pos, item) for pos, item in enumerate(result["items"])
                 if item["published"] is not None and earliest <= item["published"] <= latest]
        total = len(result["items"]) or 1
        for pos, item in fresh[:per_feed]:
            if len(item["title"].split()) < 3:  # section labels such as "Business", not headlines
                continue
            url_key = item["url"].split("#")[0].split("?")[0].rstrip("/").lower()
            title_key = (source["name"], " ".join(tokenize(item["title"])))
            if url_key in seen_urls or title_key in seen_titles:
                continue
            seen_urls.add(url_key)
            seen_titles.add(title_key)
            articles.append({
                "title": item["title"],
                "url": item["url"],
                "summary": item["summary"],
                "published": min(item["published"], now),
                "outlet": source["name"],
                "reliability": source["reliability"],
                "bias": source["bias"],
                "topic": source["topic"],
                # 1.0 for the top slot of a feed, falling towards 0 at the bottom
                "prominence": 1.0 - pos / total,
            })
    return articles


class Index:
    """TF-IDF vectors for a set of articles, with nearest-article lookup for outside text."""

    def __init__(self, articles: list[dict]):
        docs = [self._weights(a["title"], a["summary"]) for a in articles]
        self.n = len(docs)
        self.df = Counter(tok for doc in docs for tok in doc)
        self.vectors = [self._unit(doc) for doc in docs]
        self.postings: dict[str, list[int]] = defaultdict(list)
        for i, vec in enumerate(self.vectors):
            for tok in vec:
                self.postings[tok].append(i)

    @staticmethod
    def _weights(title: str, summary: str = "") -> dict[str, float]:
        """Headline words count fully, summary words at 30%."""
        weights = {tok: 0.3 for tok in set(tokenize(summary))}
        weights.update({tok: 1.0 for tok in set(tokenize(title))})
        return weights

    def _unit(self, weights: dict[str, float]) -> dict[str, float]:
        vec = {tok: w * math.log((self.n + 1) / (self.df.get(tok, 0) + 0.5)) for tok, w in weights.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {tok: v / norm for tok, v in vec.items()}

    def nearest(self, text: str) -> tuple[int, float]:
        """Most similar article to a piece of outside text: (index, similarity), or (-1, 0.0)."""
        vec = self._unit(self._weights(text))
        scores: dict[int, float] = defaultdict(float)
        for tok, w in vec.items():
            members = self.postings.get(tok, ())
            if len(members) > COMMON_TOKEN:
                continue
            for i in members:
                scores[i] += w * self.vectors[i][tok]
        if not scores:
            return -1, 0.0
        best = max(scores, key=lambda i: (scores[i], -i))
        return best, scores[best]


def _dot(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b[t] for t, v in a.items() if t in b)


def cluster(articles: list[dict], index: Index | None = None) -> tuple[list[list[int]], list[float]]:
    """Group articles about the same event.

    Returns (groups, centrality): groups are lists of article indexes, and centrality[i] says
    how typical article i is of its group (used to pick a representative headline).

    Most-similar pairs merge first; a merge is refused when the two groups as wholes are not
    similar enough, which stops loosely related stories chaining into one blob.
    """
    index = index or Index(articles)
    vectors = index.vectors
    scores: dict[tuple[int, int], float] = defaultdict(float)
    for tok, members in index.postings.items():
        if len(members) > COMMON_TOKEN:  # skipping these keeps this fast
            continue
        for x in range(len(members)):
            i = members[x]
            wi = vectors[i][tok]
            for y in range(x + 1, len(members)):
                j = members[y]
                scores[(i, j)] += wi * vectors[j][tok]
    pairs = sorted(((s, i, j) for (i, j), s in scores.items() if s >= PAIR_THRESHOLD), reverse=True)

    parent = list(range(len(articles)))
    centroid = {i: dict(v) for i, v in enumerate(vectors)}
    size = {i: 1 for i in parent}
    span = {i: (a["published"], a["published"]) for i, a in enumerate(articles)}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for _, i, j in pairs:
        a, b = find(i), find(j)
        if a == b:
            continue
        lo, hi = min(span[a][0], span[b][0]), max(span[a][1], span[b][1])
        if hi - lo > MAX_SPAN:
            continue
        ca, cb = centroid[a], centroid[b]
        norm = math.sqrt(sum(v * v for v in ca.values()) * sum(v * v for v in cb.values()))
        if _dot(ca, cb) / norm < CLUSTER_THRESHOLD:
            continue
        if size[a] < size[b]:
            a, b, ca, cb = b, a, cb, ca
        for tok, v in cb.items():
            ca[tok] = ca.get(tok, 0.0) + v
        parent[b] = a
        size[a] += size[b]
        span[a] = (lo, hi)
        del centroid[b], size[b], span[b]

    groups = defaultdict(list)
    centrality = [0.0] * len(articles)
    for i in range(len(articles)):
        root = find(i)
        groups[root].append(i)
        centrality[i] = _dot(vectors[i], centroid[root])
    return list(groups.values()), centrality


def reliability_label(score: int) -> str:
    if score >= 80:
        return "High"
    if score >= 65:
        return "Good"
    if score >= 45:
        return "Mixed"
    return "Low"


TOPIC_WORDS = {
    "tech": """ai openai chatgpt microsoft google apple nvidia meta amazon tesla spacex software app
        outage cyberattack hacker chip semiconductor robot smartphone iphone android startup tech
        technology internet online data algorithm crypto bitcoin tiktok cybersecurity""",
    "business": """market stock share economy inflation bank earning profit revenue tariff trade
        investor fed rate price oil job unemployment recession ceo merger company billion wall
        budget debt bond currency dollar retailer""",
    "science": """study scientist research climate nasa space planet species fossil vaccine virus
        disease cancer health medical drug physic telescope asteroid earthquake emission genome
        dinosaur astronomer ocean wildlife""",
    "politics": """election senate congress president minister parliament vote party democrat
        republican trump white house government bill law court supreme governor campaign poll
        labour tory senator lawmaker policy cabinet mayor""",
    "sport": """league cup match coach team season goal player championship tournament nfl nba
        football soccer cricket tennis olympic grand prix f1 golf rugby touchdown playoff""",
}
TOPIC_WORDS = {topic: frozenset(words.split()) for topic, words in TOPIC_WORDS.items()}


def classify_topic(members: list[dict]) -> str:
    """Pick a topic from headline keywords plus votes from specialist feeds; default 'world'."""
    votes: Counter = Counter()
    for art in members:
        tokens = set(tokenize(art["title"]))
        for topic, words in TOPIC_WORDS.items():
            votes[topic] += len(tokens & words)
        if art["topic"] in TOPIC_WORDS:
            votes[art["topic"]] += 2
    if not votes:
        return "world"
    topic, count = max(votes.items(), key=lambda kv: (kv[1], kv[0]))
    return topic if count >= max(2, len(members)) else "world"


def _independent_outlets(members: list[dict]) -> set[str]:
    """Outlets with a headline of their own. One that only reruns a headline already published
    by another outlet (wire copy) is not independent confirmation."""
    seen: dict[str, str] = {}
    own: set[str] = set()
    for art in sorted(members, key=lambda a: (a["published"], -a["reliability"])):
        key = " ".join(tokenize(art["title"]))
        if seen.setdefault(key, art["outlet"]) == art["outlet"]:
            own.add(art["outlet"])
    return own


def score_story(members: list[dict], now: datetime, signals: list[dict] | None = None) -> dict:
    """Score one cluster. `members` are article dicts, `signals` matched social signals."""
    signals = signals or []
    outlets: dict[str, dict] = {}
    for art in sorted(members, key=lambda a: a["published"]):
        outlets.setdefault(art["outlet"], art)  # earliest article per outlet
    n_outlets = len(outlets)
    ratings = sorted((a["reliability"] for a in outlets.values()), reverse=True)
    independent = _independent_outlets(members)

    # Reliability: the average of the three best outlets carrying the story, plus 3 points for
    # each further independent outlet rated 70+ that corroborates it (max +9). A weak outlet
    # repeating a story does not make it less true, so it neither helps nor hurts.
    base = sum(ratings[:3]) / len(ratings[:3])
    strong = sum(1 for name, a in outlets.items() if a["reliability"] >= 70 and name in independent)
    bonus = min(9, 3 * max(0, strong - 1))
    reliability = int(round(min(99, base + bonus)))

    # Trend: how widely, how recently and how prominently the story is carried, plus social buzz.
    newest = max(a["published"] for a in members)
    oldest = min(a["published"] for a in members)
    age_hours = max(0.0, (now - newest).total_seconds() / 3600)
    coverage = 1 - math.exp(-(n_outlets - 1) / 8)             # 0 for one outlet, ~0.68 at ten, ~0.9 at twenty
    recency = math.exp(-age_hours / 12)                       # halves roughly every 8 hours
    last6 = sum(1 for a in members if (now - a["published"]).total_seconds() <= 6 * 3600)
    velocity = min(1.0, last6 / 8)                            # articles in the last 6 hours
    top = sorted((a["prominence"] for a in members), reverse=True)[:3]
    prominence = sum(top) / len(top)
    best_per_platform: dict[str, dict] = {}
    for sig in signals:
        current = best_per_platform.get(sig["platform"])
        if current is None or sig["q"] > current["q"]:
            best_per_platform[sig["platform"]] = sig
    quiet = 1.0
    for sig in best_per_platform.values():
        quiet *= 1 - 0.45 * (0.5 + 0.5 * sig["q"])
    buzz = 1 - quiet                                          # 0 with no platform, ~0.83 on three
    trend = int(round(100 * (0.50 * coverage + 0.18 * buzz + 0.12 * recency + 0.12 * velocity + 0.08 * prominence)))

    sides = Counter(BIAS_SIDES[a["bias"]] for a in outlets.values())
    # Headline: the most typical article among the better-rated outlets, so the story is
    # not titled by an opinion piece or a tabloid angle.
    eligible = [a for a in members if a["reliability"] >= ratings[0] - 12]
    lead = max(eligible, key=lambda a: (a.get("centrality", 0.0) - (0.2 if LABELLED_RE.match(a["title"]) else 0.0),
                                        a["reliability"], a["published"]))

    flags = []
    if n_outlets == 1:
        flags.append("Single source")
    if ratings[0] < 45:
        flags.append("No reliable outlet")
    elif ratings[0] < 70:
        flags.append("No high-rated outlet")
    if n_outlets >= 3:
        if sides.get("left", 0) and not sides.get("right", 0) and not sides.get("center", 0):
            flags.append("Only left-leaning outlets")
        elif sides.get("right", 0) and not sides.get("left", 0) and not sides.get("center", 0):
            flags.append("Only right-leaning outlets")
        elif sides.get("state", 0) == n_outlets:
            flags.append("Only state media")

    ordered = sorted(members, key=lambda a: (-a["reliability"], -a["published"].timestamp()))
    entities = find_entities(members) if n_outlets > 1 else []
    threshold = max(2, math.ceil(0.25 * n_outlets))
    social = sorted(best_per_platform.values(), key=lambda g: (-g["q"], g["platform"]))
    return {
        "title": lead["title"],
        "url": lead["url"],
        "summary": lead["summary"] or next((a["summary"] for a in ordered if a["summary"]), ""),
        "topic": classify_topic(members),
        "trend": trend,
        "reliability": reliability,
        "reliabilityLabel": reliability_label(reliability),
        "outlets": n_outlets,
        "strongOutlets": strong,
        "copies": n_outlets - len(independent),
        "firstSeen": oldest.isoformat(timespec="minutes"),
        "lastSeen": newest.isoformat(timespec="minutes"),
        "sides": {k: sides.get(k, 0) for k in ("left", "center", "right", "state")},
        "flags": flags,
        "parts": {"base": round(base, 1), "bonus": bonus, "coverage": round(coverage, 2), "buzz": round(buzz, 2),
                  "recency": round(recency, 2), "velocity": round(velocity, 2),
                  "prominence": round(prominence, 2)},
        "account": typical_account(members) if n_outlets > 1 else [],
        "entities": [e for e in entities if e["outlets"] >= threshold][:6],
        "framing": framing(members) if n_outlets > 1 else [],
        "social": [{"platform": g["platform"], "text": g["text"], "url": g["url"], "metric": g["metric"]}
                   for g in social],
        "articles": [{"title": a["title"], "url": a["url"], "outlet": a["outlet"],
                      "reliability": a["reliability"], "bias": a["bias"],
                      "published": a["published"].isoformat(timespec="minutes")} for a in ordered],
    }


def match_signals(index: Index, groups: list[list[int]], articles: list[dict],
                  signals: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    """Attach each social signal to the story it is about. Returns (per-group signals, unmatched)."""
    group_of = {i: g for g, group in enumerate(groups) for i in group}
    by_platform: dict[str, list[dict]] = defaultdict(list)
    for sig in signals:
        by_platform[sig["platform"]].append(sig)
    for items in by_platform.values():  # q: 1.0 for the platform's biggest item, falling towards 0
        for rank, sig in enumerate(sorted(items, key=lambda x: -x["score"])):
            sig["q"] = 1 - rank / len(items)

    # Names (for Wikipedia page views) go to the story in which most outlets use that name.
    name_owner: dict[str, tuple[int, int]] = {}
    if any(sig["kind"] == "entity" for sig in signals):
        for g, group in enumerate(groups):
            members = [articles[i] for i in group]
            if len({a["outlet"] for a in members}) < 2:
                continue
            n_outlets = len({a["outlet"] for a in members})
            for ent in find_entities(members):
                key = ent["name"].lower()
                if key in GENERIC_PAGES or ent["outlets"] < 0.3 * n_outlets:
                    continue  # a passing mention does not make the story "about" that page
                if ent["outlets"] > name_owner.get(key, (0, -1))[0]:
                    name_owner[key] = (ent["outlets"], g)

    matched: list[list[dict]] = [[] for _ in groups]
    unmatched = []
    for sig in signals:
        target = -1
        sig["sim"] = 0.0
        if sig["kind"] == "entity":
            target = name_owner.get(sig["text"].lower(), (0, -1))[1]
        else:
            best = max((index.nearest(text) for text in sig["texts"] if text), key=lambda r: r[1], default=(-1, 0.0))
            sig["sim"] = best[1]
            if best[1] >= SOCIAL_THRESHOLD:
                target = group_of[best[0]]
        if target >= 0:
            matched[target].append(sig)
        else:
            unmatched.append(sig)
    return matched, unmatched


def build_stories(articles: list[dict], now: datetime,
                  signals: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Returns (stories ranked by trend, social signals that matched no story)."""
    index = Index(articles)
    groups, centrality = cluster(articles, index)
    for art, value in zip(articles, centrality):
        art["centrality"] = value
    matched, unmatched = match_signals(index, groups, articles, signals or [])
    stories = [score_story([articles[i] for i in group], now, matched[g]) for g, group in enumerate(groups)]
    stories.sort(key=lambda s: (-s["trend"], -s["reliability"], s["title"]))
    return stories, unmatched
