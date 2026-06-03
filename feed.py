# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "httpx",
#     "feedgen",
#     "feedparser",
#     "beautifulsoup4",
# ]
# ///
"""Generate an RSS feed from Reaktor's Inderes publications JSON.

State model: the previously published feed is the cache. We read it (from
FEED_URL if set, else the local OUT path) and:
  - reuse stored descriptions for guids we've already scraped, and
  - RETAIN entries that have dropped out of the source JSON (union, not replace).

Descriptions come from the release page's __NEXT_DATA__ blob (clean per-release
HTML), not from rendered markup or og:description (which is generic site-wide).
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import feedparser
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

SRC = "https://storage.googleapis.com/inderes-widgets-prod-assets/reaktor/publications"
RELEASE = "https://group.reaktor.com/release/{}"
LANG = None  # fi+en are separate items here; set to None to include both
OUT = Path("public/feed.xml")
FEED_SELF = "https://reaktor.github.io/rg.rss/feed.xml"  # canonical published location
UA = "reaktor-rss/1.0 (+https://github.com/reaktor/rg.rss)"
MAX_DESC = 600
SKIP_PREFIXES = ("reaktor group oy press release", "reaktor group oy lehdistötiedote")


def load_cache(src: str) -> dict[str, dict]:
    """guid -> full prior entry. feedparser handles a URL or a local path and
    yields no entries on 404/missing (first run)."""
    out: dict[str, dict] = {}
    for e in feedparser.parse(src).entries:
        gid = e.get("id")
        if not gid:
            continue
        pub = (
            datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            if e.get("published_parsed")
            else None
        )
        out[gid] = {
            "guid": gid,
            "title": e.get("title", "").strip(),
            "link": e.get("link", ""),
            "pubdate": pub,
            "description": e.get("summary", ""),
            "tags": [t.term for t in e.get("tags", []) if t.get("term")],
        }
    return out


def scrape_description(client: httpx.Client, url: str, release_id: str) -> str:
    """Lede paragraph from __NEXT_DATA__. Empty string if not found."""
    r = client.get(url, timeout=20)
    r.raise_for_status()
    node = BeautifulSoup(r.text, "html.parser").find("script", id="__NEXT_DATA__")
    if not node:
        return ""
    try:
        components = json.loads(node.get_text())["props"]["pageProps"]["page"]["components"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return ""

    html = title = None
    for c in components:
        if c.get("type") == "HtmlContent" and c.get("id") == release_id:
            html = c.get("props", {}).get("htmlContent")
            title = (c.get("props", {}).get("title") or "").strip()
            break
    if not html:
        return ""

    for p in BeautifulSoup(html, "html.parser").find_all("p"):
        t = " ".join(p.get_text().split())
        if not t or t.isupper() or t == title:
            continue
        if t.lower().startswith(SKIP_PREFIXES):
            continue
        return (t[:MAX_DESC].rstrip() + "\u2026") if len(t) > MAX_DESC else t
    return ""


def build() -> None:
    pubs = httpx.get(SRC, timeout=20).json()
    if LANG:
        pubs = [p for p in pubs if p.get("lang") == LANG]

    feed_url = os.environ.get("FEED_URL")
    rebuild = os.environ.get("REBUILD", "").lower() in ("1", "true", "yes")
    cache = {} if rebuild else load_cache(feed_url or str(OUT))
    records: dict[str, dict] = dict(cache)  # seed with retained (possibly-dropped) entries

    scraped = 0
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        for p in pubs:
            gid = p["_id"]
            prior = cache.get(gid)
            if prior and prior["description"] and prior["description"] != prior["title"]:
                desc = prior["description"]
            else:
                try:
                    desc = scrape_description(client, RELEASE.format(gid), gid)
                except Exception as e:  # noqa: BLE001 - degrade to title, keep building
                    print(f"scrape failed for {gid}: {e}", file=sys.stderr)
                    desc = ""
                desc = desc or p["title"].strip()
                scraped += 1
                time.sleep(1)  # polite to the origin server
            records[gid] = {
                "guid": gid,
                "title": p["title"].strip(),
                "link": RELEASE.format(gid),
                "pubdate": datetime.fromisoformat(p["publishedAt"]),
                "description": desc,
                "tags": p.get("tags", []),
            }

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    items = sorted(records.values(), key=lambda r: r["pubdate"] or epoch, reverse=True)

    fg = FeedGenerator()
    fg.id(feed_url or FEED_SELF)
    fg.title("Reaktor press releases")
    fg.link(href="https://group.reaktor.com/en/releases", rel="alternate")
    fg.link(href=feed_url or FEED_SELF, rel="self")
    fg.description("Reaktor Group press releases")
    fg.language(LANG or "fi")

    for r in items:
        fe = fg.add_entry()
        fe.guid(r["guid"], permalink=False)
        fe.title(r["title"])
        fe.link(href=r["link"])
        if r["pubdate"]:
            fe.pubDate(r["pubdate"])
        fe.description(r["description"])
        if r["tags"]:
            fe.category([{"term": t} for t in r["tags"]])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(OUT), pretty=True)
    print(f"wrote {OUT}: {len(items)} items ({len(pubs)} live, "
          f"{len(items) - len(pubs)} retained), {scraped} newly scraped")


if __name__ == "__main__":
    build()
