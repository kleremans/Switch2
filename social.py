"""Sociale bronnen: tips van andere mensen dat de Zelda Switch 2 ergens te koop is.

Bronnen (allemaal zonder account):
  - Pepper.com NL  : Nederlandse dealcommunity, gebruikers posten deals/restocks
  - Reddit         : openbare zoekfunctie (search.json)
  - Bluesky        : openbare zoek-API

Elke post wordt maar één keer gemeld. Bij de allereerste run van een bron worden
bestaande posts alleen onthouden (niet gemeld), zodat je niet overspoeld wordt
met oude berichten.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from monitor import fetch, normalize

MAX_SEEN_PER_SOURCE = 500


# --------------------------------------------------------------------------- #
# Filter
# --------------------------------------------------------------------------- #

def relevant(text: str, scfg: dict, require_nl: bool) -> bool:
    t = normalize(text)
    if not all(w in t for w in scfg["require_all"]):
        return False
    for group in scfg["require_any"]:
        if not any(w in t for w in group):
            return False
    if any(w in t for w in scfg.get("exclude", [])):
        return False
    if require_nl and not any(w in t for w in scfg["nl_words"]):
        return False
    return True


def recent(ts: float | None, max_age_hours: float) -> bool:
    if ts is None:
        return True
    return (time.time() - ts) <= max_age_hours * 3600


def iso_to_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Bronnen -> lijst van posts {id, title, text, url, ts}
# --------------------------------------------------------------------------- #

def source_pepper(session, query: str) -> tuple[str, list[dict]]:
    url = f"https://nl.pepper.com/search?q={quote_plus(query)}"
    status, html = fetch(session, url)
    if status != 200:
        return f"HTTP {status}", []
    soup = BeautifulSoup(html, "html.parser")
    posts: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        path = urlparse(href).path
        m = re.search(r"/(deals|discussies|aanbiedingen)/[^/]*?-(\d+)$", path)
        if not m or "nl.pepper.com" not in href:
            continue
        title = a.get("title") or a.get_text(" ", strip=True)
        pid = m.group(2)
        if pid not in posts or len(title) > len(posts[pid]["title"]):
            posts[pid] = {"id": pid, "title": title, "text": title + " " + path.replace("-", " "),
                          "url": href.split("?")[0], "ts": None}
    return "200", list(posts.values())


_TOKENS: dict[str, str] = {}


def _reddit_token() -> str | None:
    """OAuth-token via een (gratis) Reddit 'script'-app, als die is ingesteld."""
    cid, secret = os.getenv("REDDIT_CLIENT_ID", "").strip(), os.getenv("REDDIT_CLIENT_SECRET", "").strip()
    if not (cid and secret):
        return None
    if "reddit" not in _TOKENS:
        import requests
        r = requests.post("https://www.reddit.com/api/v1/access_token",
                          auth=(cid, secret), data={"grant_type": "client_credentials"},
                          headers={"User-Agent": "zelda-switch2-monitor/1.0"}, timeout=20)
        r.raise_for_status()
        _TOKENS["reddit"] = r.json()["access_token"]
    return _TOKENS["reddit"]


def _bluesky_token() -> str | None:
    """Sessie via Bluesky-handle + app-wachtwoord, als die is ingesteld."""
    handle, pw = os.getenv("BLUESKY_HANDLE", "").strip(), os.getenv("BLUESKY_APP_PASSWORD", "").strip()
    if not (handle and pw):
        return None
    if "bluesky" not in _TOKENS:
        import requests
        r = requests.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                          json={"identifier": handle, "password": pw}, timeout=20)
        r.raise_for_status()
        _TOKENS["bluesky"] = r.json()["accessJwt"]
    return _TOKENS["bluesky"]


def _get_json_with_token(url: str, token: str, ua: str = "zelda-switch2-monitor/1.0") -> tuple[int, str]:
    import requests
    r = requests.get(url, headers={"Authorization": f"Bearer {token}", "User-Agent": ua}, timeout=25)
    return r.status_code, r.text


def source_reddit(session, query: str) -> tuple[str, list[dict]]:
    params = f"q={quote_plus(query)}&sort=new&t=week&limit=50"
    token = _reddit_token()
    if token:
        status, body = _get_json_with_token(f"https://oauth.reddit.com/search?{params}&raw_json=1", token)
    else:
        status, body = fetch(session, f"https://www.reddit.com/search.json?{params}")
    if status != 200:
        return f"HTTP {status}", []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return "geen JSON", []
    posts = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        posts.append({
            "id": d.get("id", ""),
            "title": d.get("title", ""),
            "text": f"{d.get('title', '')} {d.get('selftext', '')} r/{d.get('subreddit', '')} {d.get('url', '')}",
            "url": "https://www.reddit.com" + d.get("permalink", ""),
            "ts": d.get("created_utc"),
        })
    return "200", posts


def source_bluesky(session, query: str) -> tuple[str, list[dict]]:
    params = f"q={quote(query)}&sort=latest&limit=50"
    token = _bluesky_token()
    if token:
        status, body = _get_json_with_token(f"https://bsky.social/xrpc/app.bsky.feed.searchPosts?{params}", token)
    else:
        status, body = fetch(session, f"https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?{params}")
    if status != 200:
        return f"HTTP {status}", []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return "geen JSON", []
    posts = []
    for p in data.get("posts", []):
        rec = p.get("record", {})
        handle = p.get("author", {}).get("handle", "")
        rkey = p.get("uri", "").rsplit("/", 1)[-1]
        links = " ".join(
            feat.get("uri", "") for facet in (rec.get("facets") or []) for feat in facet.get("features", [])
        )
        embed = (p.get("embed") or {}).get("external", {}) or {}
        posts.append({
            "id": p.get("uri", ""),
            "title": rec.get("text", "")[:120],
            "text": f"{rec.get('text', '')} {links} {embed.get('uri', '')} {embed.get('title', '')}",
            "url": f"https://bsky.app/profile/{handle}/post/{rkey}",
            "ts": iso_to_ts(rec.get("createdAt")),
        })
    return "200", posts


SOURCES = {
    "pepper": ("Pepper NL", source_pepper, False),   # al Nederlands
    "reddit": ("Reddit", source_reddit, True),       # wereldwijd -> NL-filter
    "bluesky": ("Bluesky", source_bluesky, True),    # wereldwijd -> NL-filter
}


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #

def run(session, cfg: dict, state: dict, notify, log, verbose: bool) -> None:
    scfg = cfg["social"]
    seen_all = state.setdefault("social_seen", {})
    initialized = set(state.setdefault("social_initialized", []))
    max_age = float(scfg.get("max_age_hours", 48))
    max_notify = int(scfg.get("max_notifications_per_run", 3))
    sent_count = 0

    log("\nSociale media (tips):")
    for key, (label, func, require_nl) in SOURCES.items():
        if key not in scfg.get("sources", SOURCES.keys()):
            continue
        seen = seen_all.setdefault(key, [])
        seen_set = set(seen)
        statuses, hits, total = [], [], 0
        for query in scfg["queries"]:
            try:
                status, posts = func(session, query)
            except Exception as e:  # noqa: BLE001
                status, posts = f"fout: {type(e).__name__}", []
            statuses.append(status)
            total += len(posts)
            for post in posts:
                if not post["id"] or post["id"] in seen_set:
                    continue
                if relevant(post["text"], scfg, require_nl) and recent(post["ts"], max_age):
                    hits.append(post)
                seen_set.add(post["id"])
                seen.append(post["id"])
            time.sleep(1)

        if key in ("reddit", "bluesky") and all("403" in st for st in statuses):
            statuses = ["geblokkeerd (zie README: sleutel instellen)"]
        first_run = key not in initialized
        log(f"  {label:<12} {','.join(statuses):<16} {total} posts bekeken, {len(hits)} nieuwe relevante"
            + (" (eerste run: alleen onthouden)" if first_run and hits else ""))
        for post in hits:
            if verbose or first_run:
                log(f"      - {post['title'][:90]}  {post['url']}")
            if first_run:
                continue
            if sent_count >= max_notify:
                log("      (maximum aantal tips per run bereikt)")
                break
            title = f"💬 Tip via {label}: Zelda Switch 2"
            msg = f"{post['title'][:300]}\n\nLet op: melding van een gebruiker, niet gecontroleerd.\n{post['url']}"
            notify(title, msg, post["url"], priority="high", issue=False)
            sent_count += 1

        if any(s == "200" for s in statuses):
            initialized.add(key)
        seen_all[key] = seen[-MAX_SEEN_PER_SOURCE:]

    state["social_initialized"] = sorted(initialized)
