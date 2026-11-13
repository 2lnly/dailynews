# dailynews

A daily news scraper that scores what it finds. It reads the RSS feeds of about 70 news outlets,
groups articles about the same event into one story, and gives every story two scores:

- **Trend (0–100)** – how big the story is right now.
- **Reliability (0–100)** – how far the outlets reporting it can be trusted, and how well it is corroborated.

The result is a single static page (`site/index.html`) you can sort by trending, most or least
reliable, or newest, and filter by topic, coverage and keyword. A full run takes about 3 seconds.

## Run it

Needs Python 3.10+ and nothing else (standard library only).

```sh
python -m dailynews            # writes site/index.html and site/data.json
python -m unittest discover -s tests
```

Options: `--hours 36` (look-back window), `--out site`, `--sources sources.json`, `--timeout 8`,
`--min-feeds 0.5` (exit with an error if fewer than this share of feeds could be read).

## Run it every day

`.github/workflows/daily.yml` scrapes every day at 05:17 UTC and publishes the page with GitHub Pages.
To switch it on:

1. Merge this into the default branch (GitHub only runs scheduled workflows from there).
2. In the repository, open **Settings → Pages** and set **Source** to **GitHub Actions**.
3. Optional: run it immediately from **Actions → Daily news → Run workflow**.

## How the scores work

**Trend** = 60% coverage (number of distinct outlets carrying the story) + 15% recency of the newest
article + 15% articles published in the last 6 hours + 10% how high the story sits in each feed.
A story carried by a single outlet cannot score above 28.

**Reliability** = the average rating of the three best-rated outlets reporting the story, plus
3 points for each further outlet rated 70+ that also reports it (max +9, capped at 99).
Low-rated outlets repeating a story neither raise nor lower it. Stories are also flagged when they
have a single source, no reliable outlet, or only outlets from one side of the political spectrum.

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
