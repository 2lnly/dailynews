"""Tokenising shared by clustering, matching and narrative building."""
from __future__ import annotations

import re

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
