# dailynews

A daily news scraper that scores what it finds. It reads about 160 news outlets (including the
Reuters, AP and AFP wires) and seven social and search platforms, groups articles about the same
event into one story, and for every story gives:

- **Trend (0–100)** – how big the story is right now, across outlets and social platforms.
- **Reliability (0–100)** – how far the outlets reporting it can be trusted, and how well it is corroborated.
- **The typical account** – the sentences, quoted from the outlets themselves, that best represent
  what most of the coverage says, plus the names most outlets mention and how each side of the
  political spectrum headlines it.

The result is a single static page (`site/index.html`) you can sort by trending, most or least
reliable, or newest, and filter by topic, coverage and keyword. A full run takes about 5 seconds.

## Run it

Needs Python 3.10+ and nothing else (standard library only).

```sh
python -m dailynews            # writes site/index.html and site/data.json
python -m unittest discover -s tests
```

Options: `--hours 36` (look-back window), `--out site`, `--sources sources.json`, `--timeout 8`,
`--no-social` (news outlets only), `--min-feeds 0.5` (exit with an error if fewer than this share
of feeds could be read). `site/data.json` holds the full results; the page lists every multi-outlet
story plus the 400 highest-trending single-outlet ones.

## What it reads

| Kind | Sources | Used for |
| --- | --- | --- |
| News outlets | ~160 RSS/Atom feeds listed in `sources.json`. Outlets without a usable feed of their own (Reuters, AP, AFP, CNN, The Washington Post and others) are read through Google News. | Stories, reliability, typical account |
| Social | Reddit (top news posts), Hacker News, Bluesky (trending topics and popular posts), Mastodon (trending links), Lemmy | Trend only |
| Search and reference | Google Trends (US), Wikipedia most-read pages | Trend only |

Social posts are matched to stories by wording and Wikipedia pages by name. They raise a story's
trend score and are never treated as reporting. Popular items that match no story are listed
separately as unverified. A platform that is down or blocks the request is skipped for that run.

**X (Twitter) is not included.** It has no free or anonymous way to read posts; adding it means
paying for X API access and writing a fetcher in `dailynews/social.py`.

## Run it every day

`.github/workflows/daily.yml` scrapes every day at 05:17 UTC and publishes the page with GitHub Pages.
To switch it on:

1. Merge this into the default branch (GitHub only runs scheduled workflows from there).
2. In the repository, open **Settings → Pages** and set **Source** to **GitHub Actions**.
3. Optional: run it immediately from **Actions → Daily news → Run workflow**.

## How the scores work

**Trend** = 50% coverage (number of distinct outlets carrying the story) + 18% social and search
attention (how many platforms it appears on and how high it ranks on each) + 12% recency of the
newest article + 12% articles published in the last 6 hours + 8% how high it sits in each feed.

**Reliability** = the average rating of the three best-rated outlets reporting the story, plus
3 points for each further outlet rated 70+ that reports it under its own headline (max +9, capped
at 99). An outlet that only reruns another outlet's headline (wire copy) is not counted as extra
confirmation. Low-rated outlets and social media attention neither raise nor lower it. Stories are also flagged when they
have a single source, no reliable outlet, or only outlets from one side of the political spectrum.

**Typical account** is extractive, not generated: every sentence is a quotation from an outlet's
own summary, attributed and linked. Sentences are scored by how much of their wording is shared
by the other outlets covering the story, taken only from outlets rated 60+ when there are any, and
picked from different outlets. If no outlet supplies a usable summary, the most typical headline is
shown instead. The names shown are those used by at least a quarter of the outlets.

**Grouping** uses TF-IDF similarity of headlines (and, at lower weight, summaries). The most similar
pairs merge first, and a merge is refused if the two groups as wholes are not similar enough.

## Outlet ratings

Each outlet's rating (0–100), political lean and a one-line reason live in `sources.json`.
These are editorial estimates of each outlet's track record for accuracy, sourcing and corrections.
They are a starting point, not an authority: edit the file to change a rating, or add an outlet with

```json
{"id": "example", "name": "Example News", "feed": "https://example.com/rss.xml",
 "reliability": 75, "bias": "center", "topic": "general", "note": "Why this rating."}
```

`bias` is one of `left`, `lean-left`, `center`, `lean-right`, `right`, `state`.
`topic` is `general`, `world`, `politics`, `business`, `tech` or `science`.

A rating describes the outlet, not an individual article, and a high score is not proof that a story is true.
