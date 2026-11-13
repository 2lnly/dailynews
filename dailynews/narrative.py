"""Build the typical narrative of a story from what its outlets wrote.

Nothing here is generated text. The "typical account" is made of sentences quoted from the
outlets' own summaries, chosen because they share the most with everyone else's coverage.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from .text import tokenize

SIDES = ("left", "center", "right", "state")
BIAS_SIDES = {"left": "left", "lean-left": "left", "center": "center",
              "lean-right": "right", "right": "right", "state": "state"}

SPLIT_RE = re.compile(r"(?<=[.!?])[\"”’']?\s+(?=[\"“‘']?[A-Z0-9])")
ABBREV_RE = re.compile(r"(?:\b(?:[A-Z]\.)+|\b(?:Mr|Mrs|Ms|Dr|Prof|Sen|Rep|Gov|Gen|Col|Lt|Sgt|St|Jr|Sr|Inc|Ltd|Co|Corp"
                       r"|vs|No|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.)$")
JUNK_RE = re.compile(r"read more|continue reading|click here|subscribe|newsletter|sign up|appeared first on|"
                     r"follow us|live updates|this article|photograph:|getty images|\(ap\)|\(reuters\)|^watch\b|"
                     r"your browser|all rights reserved|https?://", re.I)

FUNCTION_WORDS = frozenset("""a an the and or but if in on at to for of by with from as is are was were be been it its
    this that these those he she they we you i his her their our your my who what when where why how after before
    during while since until amid despite over under about into than then there here not no yes more most some any
    all now just also longtime still even only very will would could should can may might must do does did has have had
    one two three four five six seven eight nine ten first second last next former ex live watch video analysis
    opinion exclusive breaking update updates report reports sources say says said mr mrs ms dr sir according""".split())
NOT_NAMES = frozenset("""north south east west monday tuesday wednesday thursday friday saturday sunday january february march april june
    july august september october november december today yesterday tomorrow""".split())
CONNECTORS = frozenset(("of", "de", "del", "la", "le", "bin", "al", "van", "von", "da", "di"))
# Job titles and descriptions that sit next to names ("Nvidia CEO Jensen Huang"): they end one name
# and start the next rather than being part of either.
ROLES = frozenset("""ceo cfo president vice senator sen rep representative minister prime secretary chief governor
    gov mayor king queen prince princess judge justice general gen chairman chair director democrat republican
    democratic former interim acting billionaire founder boss leader spokesman spokeswoman envoy ambassador
    chancellor premier commissioner lt col maj capt sgt officer analyst professor author actor singer star coach
    captain""".split())
# On their own these describe rather than name ("Israeli strike"); inside a name they stay ("United Arab Emirates").
DEMONYMS = frozenset("""russian israeli chinese american british ukrainian iranian european palestinian french german indian
    japanese korean saudi turkish canadian australian mexican brazilian italian spanish syrian lebanese iraqi
    afghan pakistani taiwanese african asian arab kurdish polish dutch swedish irish scottish greek egyptian
    venezuelan cuban nigerian""".split())
DATELINE_RE = re.compile(r"^(?:[A-Z][A-Za-z .'/-]{1,40},\s*)?(?:(?:[A-Z][a-z]{2,8}\.?\s+\d{1,2}|\([A-Za-z ]{2,20}\))\s*)?"
                         r"(?:\([A-Za-z ]{2,20}\)\s*)?[-–—]\s+(?=[\"“‘']?[A-Z0-9])")
EDGE = "\"'“”‘’()[]{}<>«»…—–-*"


def sentences(text: str) -> list[str]:
    """Split into sentences without breaking on abbreviations such as 'U.S.' or 'Sen.'."""
    out: list[str] = []
    for piece in SPLIT_RE.split(text.strip()):
        if out and ABBREV_RE.search(out[-1]):
            out[-1] += " " + piece
        else:
            out.append(piece)
    out = [s.strip() for s in out if s.strip()]
    if out:  # drop a wire dateline such as "LIMA, Nov 12 - "
        out[0] = DATELINE_RE.sub("", out[0], count=1)
    return out


def _is_title_case(title: str) -> bool:
    words = [w.strip(EDGE + ".,:;!?") for w in title.split()]
    words = [w for w in words if len(w) > 3 and w[0].isalpha()]
    return len(words) >= 3 and sum(w[0].isupper() for w in words) / len(words) >= 0.7


def _names_in(sentence: str) -> list[str]:
    """Runs of capitalised words that look like proper names."""
    names: list[str] = []
    run: list[tuple[int, str]] = []  # (position in sentence, word)

    def flush() -> None:
        while run and run[0][1].lower() in FUNCTION_WORDS:
            run.pop(0)
        while run and run[-1][1].lower() in CONNECTORS | FUNCTION_WORDS:  # includes a dangling "the"
            run.pop()
        if run and len(run) <= 6:
            pos, word = run[0]
            single = len(run) == 1
            acronym = word.isupper() and len(word) > 1
            # A lone capitalised word that opens the sentence may be an ordinary word.
            ambiguous = single and pos == 0 and not acronym
            too_short = single and (len(word) < 3 or word.lower() in DEMONYMS)
            if not ambiguous and not too_short and not any(w.lower() in NOT_NAMES for _, w in run):
                names.append(" ".join(w for _, w in run))
        run.clear()

    for pos, raw in enumerate(sentence.split()):
        core = raw.rstrip(EDGE)
        possessive = raw.rstrip("\"”)]},.;:!?").endswith(("'s", "’s", "s'", "s’"))
        ends = core != core.rstrip(".,:;!?") or possessive
        word = raw.strip(EDGE + ".,:;!?")
        for suffix in ("'s", "’s"):
            if word.endswith(suffix):
                word = word[: -len(suffix)]
        word = word.rstrip("'’")
        is_cap = bool(word) and word[0].isupper() and any(c.isalpha() for c in word)
        if is_cap and word.lower().replace(".", "") in ROLES:
            flush()
        elif is_cap:
            run.append((pos, word))
        elif run and not ends and (word.lower() in CONNECTORS
                                   or (word.lower() == "the" and run[-1][1].lower() == "of")):
            run.append((pos, word))
            continue
        else:
            flush()
        if ends:
            flush()
    flush()
    return names


def _name_key(name: str) -> str:
    return name.lower().replace(".", "")


def find_entities(members: list[dict]) -> list[dict]:
    """People, places and organisations named in the coverage, with how many outlets name each.

    Only sentence-case text is used: in a Title Case Headline every word looks like a name.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    spelling: dict[str, Counter] = defaultdict(Counter)
    for art in members:
        texts = [t for t in sentences(art["summary"]) if not JUNK_RE.search(t) and not _is_title_case(t)]
        if not _is_title_case(art["title"]):
            texts.append(art["title"])
        for text in texts:
            for name in _names_in(text):
                key = _name_key(name)
                seen[key].add(art["outlet"])
                spelling[key][name] += 1
    # Fold a short form into the fuller name it belongs to ("Huang" into "Jensen Huang"), but only
    # when the fuller name is itself common, so "Gaza" is not swallowed by a rare "Gaza Peace Plan".
    for key in sorted(seen, key=lambda k: (len(k.split()), k)):
        words = key.split()
        hosts = [k for k in seen if k != key and len(k.split()) > len(words)
                 and (k.split()[-len(words):] == words or k.split()[:len(words)] == words)
                 and len(seen[k]) >= 0.4 * len(seen[key])]
        if hosts:
            host = max(hosts, key=lambda k: (len(seen[k]), k))
            seen[host] |= seen.pop(key)
    ranked = sorted(seen.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [{"name": spelling[key].most_common(1)[0][0], "outlets": len(outlets)}
            for key, outlets in ranked if len(outlets) >= 2]


def _outlet_tokens(members: list[dict]) -> dict[str, set[str]]:
    tokens: dict[str, set[str]] = defaultdict(set)
    for art in members:
        tokens[art["outlet"]].update(tokenize(art["title"] + " " + art["summary"]))
    return tokens


def typical_account(members: list[dict], max_sentences: int = 3) -> list[dict]:
    """The sentences that best represent what most outlets are saying, quoted with attribution."""
    by_outlet = _outlet_tokens(members)
    n_outlets = len(by_outlet)
    support = Counter(tok for toks in by_outlet.values() for tok in toks)
    shared = {tok: count / n_outlets for tok, count in support.items() if count >= 2}

    best_rating = max(a["reliability"] for a in members)
    floor = 60 if best_rating >= 60 else 0
    candidates = []
    for art in members:
        if art["reliability"] < floor:
            continue
        for sent in sentences(art["summary"]):
            words = len(sent.split())
            if not 8 <= words <= 55 or sent.endswith("…") or JUNK_RE.search(sent) or sent[-1] not in ".!?\"”’'":
                continue
            candidates.append((sent, set(tokenize(sent)), art))
    from_headlines = not candidates
    if not candidates:  # no usable summaries (for example wire headlines only): fall back to headlines
        for art in members:
            if art["reliability"] >= floor:
                candidates.append((art["title"], set(tokenize(art["title"])), art))
        max_sentences = 1

    def score(tokens: set[str], covered: set[str]) -> float:
        if not tokens:
            return 0.0
        gain = sum(shared.get(t, 0.0) * (0.25 if t in covered else 1.0) for t in tokens)
        return gain / math.sqrt(len(tokens) + 4)

    chosen, covered, used_outlets, first_score = [], set(), set(), 0.0
    limit = min(max_sentences, max(1, n_outlets))
    while len(chosen) < limit:
        best = None
        for sent, tokens, art in candidates:
            if art["outlet"] in used_outlets:
                continue
            if any(len(tokens & other) / max(1, len(tokens | other)) > 0.5 for _, other, _ in chosen):
                continue
            value = score(tokens, covered) * (0.6 + art["reliability"] / 250) * (1.0 + 0.2 * art.get("centrality", 0.0))
            if best is None or value > best[0]:
                best = (value, sent, tokens, art)
        if best is None or best[0] <= 0 or (chosen and best[0] < 0.45 * first_score):
            break
        value, sent, tokens, art = best
        first_score = first_score or value
        chosen.append((sent, tokens, art))
        covered |= tokens
        used_outlets.add(art["outlet"])
    return [{"text": sent, "outlet": art["outlet"], "url": art["url"], "reliability": art["reliability"],
             "headline": from_headlines} for sent, _, art in chosen]


def framing(members: list[dict]) -> list[dict]:
    """How each side of the spectrum headlines the story: its most typical headline per side."""
    side_outlets: dict[str, set[str]] = defaultdict(set)
    for art in members:
        side_outlets[BIAS_SIDES[art["bias"]]].add(art["outlet"])
    if len(side_outlets) < 2:
        return []
    out = []
    for side in SIDES:
        outlets = side_outlets.get(side)
        if not outlets:
            continue
        own = [a for a in members if a["outlet"] in outlets]
        lead = max(own, key=lambda a: (a.get("centrality", 0.0), a["reliability"], a["published"]))
        out.append({"side": side, "outlets": len(outlets), "title": lead["title"], "outlet": lead["outlet"],
                    "url": lead["url"]})
    return out
