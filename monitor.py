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
# Vroege signalen: nog niet te koop, maar er komt (binnenkort) voorraad.
COMING_PHRASES = [
    # NL
    "binnenkort beschikbaar", "binnenkort verkrijgbaar", "binnenkort weer", "verwacht binnenkort",
    "verwacht op voorraad", "weer leverbaar vanaf", "weer op voorraad vanaf", "levering verwacht",
    "verwachte leverdatum",
    # DE
    "demnächst verfügbar", "demnächst wieder", "bald wieder verfügbar", "bald verfügbar",
    "wieder lieferbar ab", "wieder verfügbar ab", "voraussichtlich lieferbar ab",
    # FR (BE)
    "bientôt disponible", "bientôt de retour", "prochainement disponible", "réassort prévu",
    # EN
    "coming soon", "back soon", "expected in stock",
]
NEGATIVE_PHRASES = [
    # NL
    "uitverkocht", "niet leverbaar", "niet op voorraad", "tijdelijk niet",
    "niet beschikbaar", "momenteel niet", "houd mij op de hoogte",
    "hou mij op de hoogte", "mail mij", "e-mail mij", "stuur mij een bericht",
    "breng me op de hoogte", "niet meer leverbaar",
    # DE
    "ausverkauft", "nicht verfügbar", "nicht lieferbar", "derzeit nicht", "nicht vorrätig",
    "nicht mehr verfügbar", "benachrichtigen sie mich", "benachrichtigung",
    # FR
    "épuisé", "rupture de stock", "indisponible", "pas disponible", "me prévenir", "m'avertir",
    # EN
    "out of stock", "sold out", "currently unavailable", "notify me",
]
POSITIVE_PHRASES = [
    # NL
    "in winkelwagen", "in mijn winkelwagen", "in winkelmand", "aan winkelwagen toevoegen",
    "toevoegen aan winkelwagen", "op voorraad", "direct leverbaar", "morgen in huis",
    "morgen bezorgd", "vandaag besteld", "voor 23:59 besteld", "nu bestellen", "bestel nu",
    "pre-order nu", "reserveer nu",
    # DE
    "in den warenkorb", "sofort lieferbar", "auf lager", "jetzt kaufen", "vorbestellen",
    # FR
    "ajouter au panier", "en stock", "commander maintenant", "précommander",
    # EN
    "add to cart", "add to basket", "buy now",
]

COUNTRIES = {"NL": ("🇳🇱", "Nederland"), "DE": ("🇩🇪", "Duitsland"), "BE": ("🇧🇪", "België")}


@dataclass
class Result:
    shop: str
    url: str
    title: str = ""
    status: str = "unknown"  # available | coming | unavailable | expensive | unknown | error
    source: str = ""  # jsonld | meta | text | http
    price: str = ""
    note: str = ""
    country: str = "NL"
    ships_nl: bool | None = True  # verzendt naar NL? None = onbekend
    shipping: float | None = None  # indicatie verzendkosten naar NL
    shop_note: str = ""

    @property
    def flag(self) -> str:
        return COUNTRIES.get(self.country, ("🌍", self.country))[0]

    def price_value(self) -> float | None:
        try:
            return float(str(self.price).replace("€", "").replace(",", ".").strip())
        except ValueError:
            return None

    def total(self) -> float | None:
        """Prijs incl. (indicatie) verzending naar NL, als de shop naar NL verzendt."""
        p = self.price_value()
        if p is None:
            return None
        return p + (self.shipping or 0) if self.ships_nl else p

    def price_text(self) -> str:
        p = self.price_value()
        if p is None:
            return ""
        if self.ships_nl and self.shipping:
            return f"€{p:.2f} + ± €{self.shipping:.0f} verzending ≈ €{self.total():.2f}"
        return f"€{p:.2f}"

    def ships_text(self) -> str:
        if self.country == "NL":
            return "Nederlandse shop"
        txt = {True: "verzendt naar NL", False: "verzendt NIET naar NL", None: "verzending naar NL onbekend"}
        return txt[self.ships_nl] + (f" ({self.shop_note})" if self.shop_note else "")


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
                if "aggregateoffer" in _types(offer) and "availability" not in offer:
                    try:
                        count = int(offer.get("offerCount") or 0)
                    except (TypeError, ValueError):
                        count = 0
                    statuses.append("instock" if count > 0 else "outofstock")
                    if count > 0:
                        price = price or str(offer.get("lowPrice") or "")
                    continue
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
    if any(p in text for p in COMING_PHRASES):
        return "coming"
    if any(p in text for p in NEGATIVE_PHRASES):
        return "unavailable"
    if any(p in text for p in POSITIVE_PHRASES):
        return "available"
    return "unknown"


def apply_max_price(res: "Result", cfg: dict) -> str:
    """Beschikbaar maar (incl. verzending) duurder dan max_price telt als 'te duur'."""
    limit = cfg.get("max_price")
    total = res.total()
    if res.status != "available" or not limit or total is None:
        return res.status
    return "expensive" if total > float(limit) else res.status


def shop_meta(shop: dict | None, url: str) -> dict:
    """Land, verzending naar NL en verzendkosten van een shop."""
    if shop is None:
        tld = urlparse(url).netloc.rsplit(".", 1)[-1].upper()
        country = tld if tld in COUNTRIES else "NL"
        return {"name": urlparse(url).netloc.removeprefix("www."), "country": country,
                "ships_to_nl": True if country == "NL" else None, "shipping_nl": None, "note": ""}
    country = shop.get("country", "NL")
    return {"name": shop["name"], "country": country,
            "ships_to_nl": shop.get("ships_to_nl", True if country == "NL" else None),
            "shipping_nl": shop.get("shipping_nl"), "note": shop.get("note", "")}


def check_product(session, meta: dict, url: str, cfg: dict, hint_title: str = "") -> Result:
    res = Result(shop=meta["name"], url=url, title=hint_title, country=meta["country"],
                 ships_nl=meta["ships_to_nl"], shipping=meta["shipping_nl"], shop_note=meta["note"])
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
    else:
        st = parse_meta(soup)
        if st:
            res.status, res.source = st, "meta"
    if res.status != "unknown":
        # Uitverkocht volgens de data, maar staat er "binnenkort weer leverbaar"? -> vroeg signaal
        if res.status == "unavailable" and parse_text(soup) == "coming":
            res.status = "coming"
        res.status = apply_max_price(res, cfg)
        return res
    if os.getenv("DEBUG_UNKNOWN"):
        res.note = debug_summary(soup)
    res.status, res.source = parse_text(soup), "text"
    res.status = apply_max_price(res, cfg)
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

def notify(title: str, message: str, click_url: str | None = None,
           priority: str = "urgent", issue: bool = True) -> list[str]:
    sent: list[str] = []
    import requests  # gewone requests volstaat voor meldingen

    topic = (os.getenv("NTFY_TOPIC") or "").strip()
    # Accepteer ook een volledige URL zoals "https://ntfy.sh/mijn-onderwerp"
    if topic.startswith("http"):
        server, _, topic = topic.rstrip("/").rpartition("/")
    else:
        server = (os.getenv("NTFY_SERVER") or "").strip() or "https://ntfy.sh"
    topic = topic.strip("/ ")
    if topic:
        server = server.rstrip("/")
        headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": "rotating_light,video_game"}
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
    if issue and gh_token and repo and os.getenv("GITHUB_ISSUE_ALERTS", "true").lower() == "true":
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
    candidates: dict[str, tuple[dict, str]] = {}  # url -> (shop-meta, titel)
    shops_by_host = {urlparse(s["search"]).netloc.removeprefix("www."): s for s in cfg["shops"]}
    for url in cfg.get("product_urls") or []:
        host = urlparse(url).netloc.removeprefix("www.")
        candidates[clean_url(url)] = (shop_meta(shops_by_host.get(host), url), "")

    reports: list[ShopReport] = []
    searched_ok: set[str] = set()  # shops waarvan de zoekpagina werkte
    for shop in cfg["shops"]:
        if shop.get("enabled") is False:
            continue
        rep = ShopReport(f"{COUNTRIES.get(shop.get('country', 'NL'), ('', ''))[0]} {shop['name']}")
        per_shop: dict[str, str] = {}
        for term in cfg["search_terms"]:
            url = shop["search"].replace("{q}", quote_plus(term))
            status, html = fetch(session, url)
            rep.search_status.append(str(status))
            if status == 200:
                for cu, title in find_candidates(html, url, cfg):
                    per_shop.setdefault(cu, title)
            time.sleep(random.uniform(0.5, 1.5))
        if "200" in rep.search_status:
            searched_ok.add(shop["name"])
        for cu, title in list(per_shop.items())[: cfg.get("max_candidates_per_shop", 6)]:
            candidates.setdefault(cu, (shop_meta(shop, cu), title))
            rep.candidates.append(cu)
        reports.append(rep)

    # 2. Controleer elke productpagina
    results: list[Result] = []
    for url, (meta, title) in candidates.items():
        res = check_product(session, meta, url, cfg, title)
        results.append(res)
        time.sleep(random.uniform(0.5, 1.5))

    # 3. Rapportage
    log("=" * 70)
    log(f"Zelda Switch 2 monitor - {now}")
    log("=" * 70)
    log("Zoekpagina's (HTTP-status per zoekterm / aantal kandidaten):")
    for rep in reports:
        log(f"  {rep.shop:<24} {','.join(rep.search_status):<12} {len(rep.candidates)} kandidaat(en)")
        if verbose:
            for c in rep.candidates:
                log(f"      - {c}")
    log("")
    log("Productpagina's:")
    icon = {"available": "✅ BESCHIKBAAR", "coming": "👀 binnenkort", "unavailable": "❌ uitverkocht",
            "unknown": "❔ onbekend", "expensive": "💸 te duur", "error": "⚠️  fout"}
    for r in results:
        extra = f" {r.price_text()}" if r.price else ""
        log(f"  {icon[r.status]:<16} {r.flag} [{r.shop}] {r.title[:55]}{extra} ({r.source} {r.note})".rstrip())
        log(f"      {r.ships_text()} - {r.url}")

    # 4. Meldingen bij overgang naar beschikbaar
    products = state.setdefault("products", {})
    # Een nieuwe productpagina is pas een signaal als we die shop al eerder bekeken
    # (anders geeft elke nieuw toegevoegde shop een stortvloed aan meldingen).
    known_shops = set(state.get("known_shops") or {p.get("shop") for p in products.values()})
    new_hits: list[Result] = []
    signals: list[tuple[Result, str]] = []
    for r in results:
        prev = products.get(r.url)
        prev_status = (prev or {}).get("status")
        if r.status == "available" and prev_status != "available":
            new_hits.append(r)
        elif r.status == "coming" and prev_status not in ("coming", "available"):
            signals.append((r, "De shop meldt dat de console binnenkort (weer) leverbaar is."))
        elif prev is None and r.shop in known_shops and r.status not in ("error",):
            signals.append((r, "Nieuwe productpagina gevonden - vaak een voorbode van (nieuwe) voorraad."))
        if r.status in ("available", "coming", "unavailable", "expensive") or prev is None:
            products[r.url] = {
                "shop": r.shop, "country": r.country, "title": r.title, "status": r.status,
                "source": r.source, "price": r.price,
                "last_change": now if prev_status != r.status else (prev or {}).get("last_change", now),
            }

    state["known_shops"] = sorted(known_shops | searched_ok
                                  | {r.shop for r in results if r.status != "error"})

    def details(r: Result) -> str:
        country = COUNTRIES.get(r.country, ("", r.country))[1]
        lines = [r.title or "Nintendo Switch 2 Zelda 40th Anniversary Edition",
                 f"Shop: {r.shop} {r.flag} {country} - {r.ships_text()}"]
        if r.price:
            lines.append(f"Prijs: {r.price_text()}")
        return "\n".join(lines)

    for r in new_hits:
        certainty = "" if r.source in ("jsonld", "meta") else " (waarschijnlijk - controleer even)"
        title = f"🚨 {r.flag} Zelda Switch 2 beschikbaar bij {r.shop}!"
        msg = f"{details(r)}\nStatus: op voorraad{certainty}\n{r.url}"
        log(f"\n>>> MELDING: {title} -> {r.url}")
        sent = notify(title, msg, r.url)
        log(f"    verstuurd via: {', '.join(sent) or 'niets'}")

    if (cfg.get("early_signals") or {}).get("enabled", True):
        for r, why in signals:
            title = f"👀 {r.flag} Vroeg signaal: Zelda Switch 2 bij {r.shop}"
            msg = f"{details(r)}\n{why}\n{r.url}"
            log(f"\n>>> VROEG SIGNAAL: {title} -> {r.url}")
            sent = notify(title, msg, r.url, priority="high", issue=False)
            log(f"    verstuurd via: {', '.join(sent) or 'niets'}")

    # 5. Sociale media (tips van andere mensen)
    if (cfg.get("social") or {}).get("enabled"):
        import social
        social.run(session, cfg, state, notify, log, verbose)

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
