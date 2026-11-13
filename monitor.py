#!/usr/bin/env python3
"""Voorraad-monitor voor de Nintendo Switch 2 Zelda 40th Anniversary Edition.

Doorzoekt Nederlandse webshops, bekijkt gevonden productpagina's en stuurt een
melding (ntfy push, Telegram en/of GitHub issue) zodra een productpagina van
"niet beschikbaar" naar "beschikbaar" gaat.

Gebruik:
    python monitor.py               # normale run
    python monitor.py --test-notify # stuur alleen een testmelding
    python monitor.py --verbose     # uitgebreide log per shop
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

import yaml
from bs4 import BeautifulSoup

try:  # curl_cffi imiteert een echte Chrome-browser -> minder vaak geblokkeerd
    from curl_cffi import requests as http

    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # pragma: no cover
    import requests as http  # type: ignore

    IMPERSONATE = {}

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
}
TIMEOUT = 25

# schema.org availability -> bestelbaar?
AVAILABLE_SCHEMA = {
    "instock", "limitedavailability", "onlineonly", "preorder", "presale", "backorder",
}
UNAVAILABLE_SCHEMA = {"outofstock", "soldout", "discontinued", "instoreonly"}

# Tekst-heuristiek als er geen gestructureerde data is.
NEGATIVE_PHRASES = [
    "uitverkocht", "niet leverbaar", "niet op voorraad", "tijdelijk niet",
    "niet beschikbaar", "momenteel niet", "houd mij op de hoogte",
    "hou mij op de hoogte", "mail mij", "e-mail mij", "stuur mij een bericht",
    "breng me op de hoogte", "binnenkort verkrijgbaar", "binnenkort beschikbaar",
    "out of stock", "sold out", "currently unavailable", "niet meer leverbaar",
    "notify me", "verwacht binnenkort",
]
POSITIVE_PHRASES = [
    "in winkelwagen", "in mijn winkelwagen", "in winkelmand", "aan winkelwagen toevoegen",
    "toevoegen aan winkelwagen", "op voorraad", "direct leverbaar", "morgen in huis",
    "morgen bezorgd", "vandaag besteld", "voor 23:59 besteld", "add to cart",
    "add to basket", "nu bestellen", "bestel nu", "pre-order nu", "reserveer nu",
]


@dataclass
class Result:
    shop: str
    url: str
    title: str = ""
    status: str = "unknown"  # available | unavailable | unknown | error
    source: str = ""  # jsonld | meta | text | http
    price: str = ""
    note: str = ""


@dataclass
class ShopReport:
    shop: str
    search_status: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"products": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                          encoding="utf-8")


def normalize(text: str) -> str:
    text = text.lower().replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text)


def matches(text: str, cfg: dict) -> bool:
    t = normalize(text)
    m = cfg["match"]
    if not all(w in t for w in m["require_all"]):
        return False
    for group in m["require_any"]:
        if not any(w in t for w in group):
            return False
    return not any(w in t for w in m["exclude"])


def clean_url(url: str) -> str:
    """Verwijder query/fragment zodat dezelfde pagina één sleutel krijgt."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", "", ""))


def fetch(session, url: str) -> tuple[int, str]:
    try:
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, **IMPERSONATE)
        return r.status_code, r.text or ""
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# Zoekpagina -> kandidaat-productlinks
# --------------------------------------------------------------------------- #

URL_IN_TEXT = re.compile(r'(?:"|\\u002F|\\/|/)[^"\s<>]*?zelda[^"\s<>]*', re.I)


def find_candidates(html: str, base_url: str, cfg: dict) -> list[tuple[str, str]]:
    """Geef (url, titel) terug voor links die op het product lijken."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.split(":")[0].removeprefix("www.")
    found: dict[str, str] = {}

    def consider(href: str, label: str) -> None:
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            return
        href = href.replace("\\u002F", "/").replace("\\/", "/").rstrip("\\")
        full = urljoin(base_url, href)
        host = urlparse(full).netloc.removeprefix("www.")
        if base_host not in host:
            return
        key = clean_url(full)
        if any(part in key for part in cfg.get("exclude_url_parts") or []):
            return
        text = f"{label} {urlparse(full).path.replace('-', ' ')} {urlparse(full).path}"
        if matches(text, cfg) and "zoeken" not in key and "search" not in key:
            if key not in found or len(label) > len(found[key]):
                found[key] = label.strip()

    for a in soup.find_all("a", href=True):
        label = " ".join(filter(None, [a.get_text(" ", strip=True), a.get("title", ""),
                                       a.get("aria-label", "")]))
        consider(a["href"], label)

    # Links die in JSON/JS-blobs zitten (Next.js-shops, etc.)
    for m in URL_IN_TEXT.finditer(html):
        raw = m.group(0).strip('"')
        raw = raw.replace("\\u002F", "/").replace("\\/", "/")
        if raw.startswith("http") or raw.startswith("/"):
            consider(raw, "")

    return list(found.items())


# --------------------------------------------------------------------------- #
# Productpagina -> beschikbaarheid
# --------------------------------------------------------------------------- #

def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    if isinstance(t, str):
        t = [t]
    return {str(x).lower() for x in t}


def _avail_token(value) -> str:
    return str(value).rstrip("/").split("/")[-1].lower()


def parse_jsonld(soup: BeautifulSoup, cfg: dict) -> tuple[str | None, str, str]:
    """Return (status, titel, prijs) op basis van schema.org JSON-LD."""
    statuses: list[str] = []
    title = price = ""
    for script in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(data):
            if not ({"product", "productgroup"} & _types(node)):
                continue
            name = str(node.get("name", ""))
            if name and not matches(name, cfg):
                continue  # gerelateerd product (bv. controller) negeren
            title = title or name
            offers = node.get("offers") or []
            for offer in _walk(offers):
                if "availability" in offer:
                    statuses.append(_avail_token(offer["availability"]))
                    price = price or str(offer.get("price") or offer.get("lowPrice") or "")
    if not statuses:
        return None, title, price
    if any(s in AVAILABLE_SCHEMA for s in statuses):
        return "available", title, price
    if any(s in UNAVAILABLE_SCHEMA for s in statuses):
        return "unavailable", title, price
    return None, title, price


def parse_meta(soup: BeautifulSoup) -> str | None:
    for attr in ("product:availability", "og:availability", "availability"):
        tag = soup.find("meta", attrs={"property": attr}) or soup.find("meta", attrs={"itemprop": attr})
        if tag and tag.get("content"):
            tok = _avail_token(tag["content"]).replace(" ", "")
            if tok in AVAILABLE_SCHEMA or tok in {"instock", "available"}:
                return "available"
            if tok in UNAVAILABLE_SCHEMA or tok in {"oos", "outofstock"}:
                return "unavailable"
    for tag in soup.find_all(attrs={"itemprop": "availability"}):
        tok = _avail_token(tag.get("href") or tag.get("content") or "")
        if tok in AVAILABLE_SCHEMA:
            return "available"
        if tok in UNAVAILABLE_SCHEMA:
            return "unavailable"
    return None


def parse_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "template", "svg", "footer", "nav"]):
        tag.decompose()
    text = normalize(soup.get_text(" "))
    if any(p in text for p in NEGATIVE_PHRASES):
        return "unavailable"
    if any(p in text for p in POSITIVE_PHRASES):
        return "available"
    return "unknown"


def check_product(session, shop: str, url: str, cfg: dict, hint_title: str = "") -> Result:
    res = Result(shop=shop, url=url, title=hint_title)
    status, html = fetch(session, url)
    if status != 200:
        res.status, res.source, res.note = "error", "http", f"HTTP {status}"
        return res
    soup = BeautifulSoup(html, "html.parser")
    if not res.title:
        h1 = soup.find("h1")
        og = soup.find("meta", attrs={"property": "og:title"})
        res.title = (h1.get_text(" ", strip=True) if h1 else "") or (og.get("content", "") if og else "")

    st, title, price = parse_jsonld(soup, cfg)
    res.title = title or res.title
    res.price = price
    if st:
        res.status, res.source = st, "jsonld"
        return res
    st = parse_meta(soup)
    if st:
        res.status, res.source = st, "meta"
        return res
    if os.getenv("DEBUG_UNKNOWN"):
        res.note = debug_summary(soup)
    res.status, res.source = parse_text(soup), "text"
    return res


def debug_summary(soup: BeautifulSoup) -> str:
    """Compacte samenvatting van een pagina zonder herkenbare voorraadinfo."""
    parts = []
    for script in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(data):
            if "offers" in node or {"offer", "aggregateoffer"} & _types(node):
                parts.append("LD:" + json.dumps(node.get("offers", node), ensure_ascii=False)[:300])
    text = normalize(soup.get_text(" "))
    for m in list(re.finditer("€", text))[:5]:
        parts.append("TXT:" + text[max(0, m.start() - 60): m.end() + 30])
    return " | ".join(parts)[:1500]


# --------------------------------------------------------------------------- #
# Meldingen
# --------------------------------------------------------------------------- #

def notify(title: str, message: str, click_url: str | None = None) -> list[str]:
    sent: list[str] = []
    import requests  # gewone requests volstaat voor meldingen

    topic = os.getenv("NTFY_TOPIC", "").strip()
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {"Title": title.encode("utf-8"), "Priority": "urgent", "Tags": "rotating_light,video_game"}
        if click_url:
            headers["Click"] = click_url
            headers["Actions"] = f"view, Open webshop, {click_url}"
        try:
            r = requests.post(f"{server}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=20)
            r.raise_for_status()
            sent.append("ntfy")
        except Exception as e:  # noqa: BLE001
            log(f"!! ntfy-melding mislukt: {e}")

    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": f"{title}\n\n{message}", "disable_web_page_preview": False},
                timeout=20,
            )
            r.raise_for_status()
            sent.append("telegram")
        except Exception as e:  # noqa: BLE001
            log(f"!! Telegram-melding mislukt: {e}")

    gh_token, repo = os.getenv("GITHUB_TOKEN", "").strip(), os.getenv("GITHUB_REPOSITORY", "").strip()
    if gh_token and repo and os.getenv("GITHUB_ISSUE_ALERTS", "true").lower() == "true":
        try:
            r = requests.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"},
                json={"title": title, "body": message},
                timeout=20,
            )
            r.raise_for_status()
            sent.append("github-issue")
        except Exception as e:  # noqa: BLE001
            log(f"!! GitHub-issue aanmaken mislukt: {e}")

    if not sent:
        log("!! Geen meldingskanaal geconfigureerd of alle kanalen faalden.")
    return sent


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run(verbose: bool) -> int:
    cfg = load_config()
    state = load_state()
    session = http.Session()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 1. Verzamel kandidaten: vaste URLs + zoekresultaten per shop
    candidates: dict[str, tuple[str, str]] = {}  # url -> (shop, titel)
    for url in cfg.get("product_urls") or []:
        host = urlparse(url).netloc.removeprefix("www.")
        candidates[clean_url(url)] = (host, "")

    reports: list[ShopReport] = []
    for shop in cfg["shops"]:
        if shop.get("enabled") is False:
            continue
        rep = ShopReport(shop["name"])
        per_shop: dict[str, str] = {}
        for term in cfg["search_terms"]:
            url = shop["search"].replace("{q}", quote_plus(term))
            status, html = fetch(session, url)
            rep.search_status.append(str(status))
            if status == 200:
                for cu, title in find_candidates(html, url, cfg):
                    per_shop.setdefault(cu, title)
            time.sleep(random.uniform(0.5, 1.5))
        for cu, title in list(per_shop.items())[: cfg.get("max_candidates_per_shop", 6)]:
            candidates.setdefault(cu, (shop["name"], title))
            rep.candidates.append(cu)
        reports.append(rep)

    # 2. Controleer elke productpagina
    results: list[Result] = []
    for url, (shop, title) in candidates.items():
        res = check_product(session, shop, url, cfg, title)
        results.append(res)
        time.sleep(random.uniform(0.5, 1.5))

    # 3. Rapportage
    log("=" * 70)
    log(f"Zelda Switch 2 monitor - {now}")
    log("=" * 70)
    log("Zoekpagina's (HTTP-status per zoekterm / aantal kandidaten):")
    for rep in reports:
        log(f"  {rep.shop:<22} {','.join(rep.search_status):<12} {len(rep.candidates)} kandidaat(en)")
        if verbose:
            for c in rep.candidates:
                log(f"      - {c}")
    log("")
    log("Productpagina's:")
    icon = {"available": "✅ BESCHIKBAAR", "unavailable": "❌ uitverkocht", "unknown": "❔ onbekend",
            "error": "⚠️  fout"}
    for r in results:
        extra = f" €{r.price}" if r.price else ""
        log(f"  {icon[r.status]:<16} [{r.shop}] {r.title[:60]}{extra} ({r.source} {r.note})".rstrip())
        log(f"      {r.url}")

    # 4. Meldingen bij overgang naar beschikbaar
    products = state.setdefault("products", {})
    new_hits: list[Result] = []
    for r in results:
        prev = products.get(r.url, {})
        if r.status in ("available", "unavailable"):
            if r.status == "available" and prev.get("status") != "available":
                new_hits.append(r)
            products[r.url] = {
                "shop": r.shop, "title": r.title, "status": r.status, "source": r.source,
                "price": r.price, "last_change": now if prev.get("status") != r.status else prev.get("last_change", now),
            }
        elif r.url not in products:
            products[r.url] = {"shop": r.shop, "title": r.title, "status": r.status, "last_change": now}

    for r in new_hits:
        certainty = "" if r.source in ("jsonld", "meta") else " (waarschijnlijk - controleer even)"
        title = f"🚨 Zelda Switch 2 beschikbaar bij {r.shop}!"
        msg = (f"{r.title or 'Nintendo Switch 2 Zelda 40th Anniversary Edition'}\n"
               f"{'Prijs: €' + r.price + chr(10) if r.price else ''}"
               f"Status: op voorraad{certainty}\n{r.url}")
        log(f"\n>>> MELDING: {title} -> {r.url}")
        sent = notify(title, msg, r.url)
        log(f"    verstuurd via: {', '.join(sent) or 'niets'}")

    state["last_run"] = now
    save_state(state)
    if not new_hits:
        log("\nGeen nieuwe beschikbaarheid gevonden.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test-notify", action="store_true", help="stuur alleen een testmelding")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    if args.test_notify:
        sent = notify("✅ Testmelding Zelda Switch 2 monitor",
                      "Als je dit leest, werken de meldingen. Je krijgt een bericht zodra de "
                      "Zelda-editie ergens op voorraad is.",
                      "https://github.com/" + os.getenv("GITHUB_REPOSITORY", ""))
        log(f"Testmelding verstuurd via: {', '.join(sent) or 'niets'}")
        return 0 if sent else 1
    return run(args.verbose)


if __name__ == "__main__":
    sys.exit(main())
