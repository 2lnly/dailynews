"""Turn fetched articles into ranked stories: dedupe, cluster, then score trend and reliability."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
STOPWORDS = frozenset(
    """a about after again against all also am an and any are as at be because been before being between both
    but by can could did do does doing down during each few for from further had has have having he her here hers
    him his how i if in into is it its just me more most my new no nor not now of off on once only or other our out
    over own s same she should so some such t than that the their them then there these they this those through to
    too under until up us very was we were what when where which while who whom why will with would you your
    say says said saying told tells live update updates latest news video watch breaking report reports reported
    amid here's what's know need first one two three year years day days week time may might must make makes
    made get gets got take takes back still way set show shows per via vs mr mrs ms""".split()
)

# Clustering thresholds, tuned by eye on real feeds: lower merges more, higher splits more.
PAIR_THRESHOLD = 0.34      # two articles may be linked at or above this similarity
CLUSTER_THRESHOLD = 0.30   # the two groups being merged must also be this similar as wholes
MAX_SPAN = timedelta(hours=48)

BIAS_SIDES = {"left": "left", "lean-left": "left", "center": "center",
              "lean-right": "right", "right": "right", "state": "state"}


def tokenize(text: str) -> list[str]:
    tokens = []
    for raw in TOKEN_RE.findall(text.lower()):
        token = raw.replace("’", "'")
        if token.endswith("'s"):
            token = token[:-2]
        token = token.replace("'", "")
        if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
            token = token[:-1]
        if len(token) > 1 and token not in STOPWORDS:
            tokens.append(token)
    return tokens


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


def _vectors(articles: list[dict]) -> list[dict[str, float]]:
    """Unit-length TF-IDF vectors; headline words count fully, summary words at 30%."""
    docs = []
    for art in articles:
        weights: dict[str, float] = {}
        for tok in set(tokenize(art["summary"])):
            weights[tok] = 0.3
        for tok in set(tokenize(art["title"])):
            weights[tok] = 1.0
        docs.append(weights)
    df = Counter(tok for doc in docs for tok in doc)
    n = len(docs)
    vectors = []
    for doc in docs:
        vec = {tok: w * math.log((n + 1) / (df[tok] + 0.5)) for tok, w in doc.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({tok: v / norm for tok, v in vec.items()})
    return vectors


def _dot(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b[t] for t, v in a.items() if t in b)


def cluster(articles: list[dict]) -> tuple[list[list[int]], list[float]]:
    """Group articles about the same event.

    Returns (groups, centrality): groups are lists of article indexes, and centrality[i] says
    how typical article i is of its group (used to pick a representative headline).

    Most-similar pairs merge first; a merge is refused when the two groups as wholes are not
    similar enough, which stops loosely related stories chaining into one blob.
    """
    vectors = _vectors(articles)
    index = defaultdict(list)
    for i, vec in enumerate(vectors):
        for tok in vec:
            index[tok].append(i)
    scores: dict[tuple[int, int], float] = defaultdict(float)
    for tok, members in index.items():
        if len(members) > 400:  # too common to discriminate; skipping keeps this fast
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


def score_story(members: list[dict], now: datetime) -> dict:
    """Score one cluster. `members` are article dicts; returns the story record."""
    outlets: dict[str, dict] = {}
    for art in sorted(members, key=lambda a: a["published"]):
        outlets.setdefault(art["outlet"], art)  # earliest article per outlet
    n_outlets = len(outlets)
    ratings = sorted((a["reliability"] for a in outlets.values()), reverse=True)

    # Reliability: the average of the three best outlets carrying the story, plus 3 points for
    # each further outlet rated 70+ that corroborates it (max +9). A weak outlet repeating a
    # story does not make it less true, so it neither helps nor hurts.
    base = sum(ratings[:3]) / len(ratings[:3])
    strong = sum(1 for r in ratings if r >= 70)
    bonus = min(9, 3 * max(0, strong - 1))
    reliability = int(round(min(99, base + bonus)))

    # Trend: how widely, how recently and how prominently the story is being carried.
    newest = max(a["published"] for a in members)
    oldest = min(a["published"] for a in members)
    age_hours = max(0.0, (now - newest).total_seconds() / 3600)
    coverage = 1 - math.exp(-(n_outlets - 1) / 5)             # 0 for one outlet, ~0.83 at ten
    recency = math.exp(-age_hours / 12)                       # halves roughly every 8 hours
    last6 = sum(1 for a in members if (now - a["published"]).total_seconds() <= 6 * 3600)
    velocity = min(1.0, last6 / 5)                            # articles in the last 6 hours
    prominence = sum(sorted((a["prominence"] for a in members), reverse=True)[:3]) / 3 \
        if len(members) >= 3 else sum(a["prominence"] for a in members) / len(members)
    trend = int(round(100 * (0.60 * coverage + 0.15 * recency + 0.15 * velocity + 0.10 * prominence)))

    sides = Counter(BIAS_SIDES[a["bias"]] for a in outlets.values())
    # Headline: the most typical article among the better-rated outlets, so the story is
    # not titled by an opinion piece or a tabloid angle.
    eligible = [a for a in members if a["reliability"] >= ratings[0] - 12]
    lead = max(eligible, key=lambda a: (a.get("centrality", 0.0), a["reliability"], a["published"]))
    topic = classify_topic(members)

    flags = []
    if n_outlets == 1:
        flags.append("Single source")
    if ratings[0] < 45:
        flags.append("No reliable outlet")
    elif strong == 0:
        flags.append("No high-rated outlet")
    if n_outlets >= 3:
        if sides.get("left", 0) and not sides.get("right", 0) and not sides.get("center", 0):
            flags.append("Only left-leaning outlets")
        elif sides.get("right", 0) and not sides.get("left", 0) and not sides.get("center", 0):
            flags.append("Only right-leaning outlets")
        elif sides.get("state", 0) == n_outlets:
            flags.append("Only state media")

    ordered = sorted(members, key=lambda a: (-a["reliability"], -a["published"].timestamp()))
    return {
        "title": lead["title"],
        "url": lead["url"],
        "summary": lead["summary"] or next((a["summary"] for a in ordered if a["summary"]), ""),
        "topic": topic,
        "trend": trend,
        "reliability": reliability,
        "reliabilityLabel": reliability_label(reliability),
        "outlets": n_outlets,
        "strongOutlets": strong,
        "firstSeen": oldest.isoformat(timespec="minutes"),
        "lastSeen": newest.isoformat(timespec="minutes"),
        "sides": {k: sides.get(k, 0) for k in ("left", "center", "right", "state")},
        "flags": flags,
        "parts": {"base": round(base, 1), "bonus": bonus, "coverage": round(coverage, 2),
                  "recency": round(recency, 2), "velocity": round(velocity, 2),
                  "prominence": round(prominence, 2)},
        "articles": [{"title": a["title"], "url": a["url"], "outlet": a["outlet"],
                      "reliability": a["reliability"], "bias": a["bias"],
                      "published": a["published"].isoformat(timespec="minutes")} for a in ordered],
    }


def build_stories(articles: list[dict], now: datetime) -> list[dict]:
    groups, centrality = cluster(articles)
    for art, value in zip(articles, centrality):
        art["centrality"] = value
    stories = [score_story([articles[i] for i in group], now) for group in groups]
    stories.sort(key=lambda s: (-s["trend"], -s["reliability"], s["title"]))
    return stories
