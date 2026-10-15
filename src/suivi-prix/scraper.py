"""Lecture du prix d'un article sur Amazon, eBay et Leboncoin.

Amazon et Leboncoin se lisent en direct ; si la page est bloquée, on retente via
FlareSolverr quand il est configuré. eBay bloque la lecture de ses pages, même via
FlareSolverr : on passe par son API officielle (Browse API), avec les clés du compte
développeur de l'utilisateur.
"""
import base64
import gzip
import html
import json
import re
import threading
import unicodedata
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, quote_plus, urlencode, urlparse

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

# Indices de page anti-robot (captcha, DataDome, page d'erreur eBay...).
BLOCK_MARKERS = [
    "captcha",
    "datadome",
    "geo.captcha-delivery.com",
    "robot check",
    "pardon our interruption",
    "api-services-support@amazon.com",
    "error page | ebay",
]

SITES = {"amazon": "Amazon", "ebay": "eBay", "leboncoin": "Leboncoin"}


class ScrapeError(Exception):
    """Prix illisible : page bloquée, introuvable ou de structure inattendue."""


def site_of(url):
    host = urlparse(url).netloc.lower()
    for name in SITES:
        if name in host:
            return name
    return None


def clean_url(url):
    """Retire les paramètres de pistage : https://www.amazon.fr/.../dp/B0XX?pd_rd=... -> /dp/B0XX."""
    url = url.strip()
    site = site_of(url)
    if site == "amazon":
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
        if m:
            host = urlparse(url).netloc
            return f"https://{host}/dp/{m.group(1)}"
    if site == "ebay":
        m = re.search(r"/itm/(?:[^/?]+/)?(\d{9,})", url)
        if m:
            host = urlparse(url).netloc
            return f"https://{host}/itm/{m.group(1)}"
    if site == "leboncoin":
        return url.split("?")[0].split("#")[0]
    return url


# --- Récupération ---------------------------------------------------------

def fetch_direct(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw, enc, status = resp.read(), resp.headers.get("Content-Encoding", ""), resp.status
    except urllib.error.HTTPError as e:
        raw, enc, status = e.read(), e.headers.get("Content-Encoding", ""), e.code
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    return status, raw.decode("utf-8", errors="replace")


def fetch_flaresolverr(url, endpoint):
    payload = json.dumps({"cmd": "request.get", "url": url, "maxTimeout": 60000}).encode()
    req = urllib.request.Request(endpoint.rstrip("/") + "/v1", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.load(resp)
    sol = data.get("solution") or {}
    return sol.get("status", 0), sol.get("response", "")


# --- API eBay ---------------------------------------------------------------

EBAY_API = "https://api.ebay.com"
_ebay_token = {"key": None, "value": None, "expires": 0}
_ebay_lock = threading.Lock()


def ebay_token(client_id, client_secret):
    """Jeton d'application OAuth (valable 2 h), mis en cache."""
    key = (client_id, client_secret)
    with _ebay_lock:
        if _ebay_token["key"] == key and _ebay_token["expires"] > time.time() + 60:
            return _ebay_token["value"]
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        body = urlencode({"grant_type": "client_credentials",
                          "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
        req = urllib.request.Request(EBAY_API + "/identity/v1/oauth2/token", data=body, headers={
            "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            raise ScrapeError(f"Clés eBay refusées (HTTP {e.code}) : vérifiez l'App ID et le Cert ID") from e
        _ebay_token.update(key=key, value=data["access_token"], expires=time.time() + int(data.get("expires_in", 7200)))
        return _ebay_token["value"]


def ebay_get(path, token, marketplace):
    req = urllib.request.Request(EBAY_API + path, headers={
        "Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace,
        "Accept-Language": "fr-FR"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            errors = json.loads(e.read()).get("errors") or []
        except ValueError:
            errors = []
        return {"_status": e.code, "_errors": errors}


def ebay_marketplace(url):
    host = urlparse(url).netloc.lower()
    for suffix, market in [(".fr", "EBAY_FR"), (".de", "EBAY_DE"), (".it", "EBAY_IT"), (".es", "EBAY_ES"),
                           (".co.uk", "EBAY_GB"), (".be", "EBAY_BE"), (".com", "EBAY_US")]:
        if host.endswith(suffix):
            return market
    return "EBAY_FR"


def check_ebay_api(url, client_id, client_secret):
    m = re.search(r"/itm/(?:[^/?]+/)?(\d{9,})", url)
    if not m:
        raise ScrapeError("Lien eBay sans numéro d'article (/itm/...)")
    legacy_id, market = m.group(1), ebay_marketplace(url)
    token = ebay_token(client_id, client_secret)
    data = ebay_get(f"/buy/browse/v1/item/get_item_by_legacy_id?legacy_item_id={quote(legacy_id)}", token, market)
    if data.get("_status"):
        codes = {err.get("errorId") for err in data["_errors"]}
        if 11006 in codes:
            # Annonce à variantes (tailles, couleurs...) : on retient le prix le plus bas.
            group = ebay_get(f"/buy/browse/v1/item/get_items_by_item_group?item_group_id={quote(legacy_id)}",
                             token, market)
            items = group.get("items") or []
            priced = [i for i in items if parse_price((i.get("price") or {}).get("value")) is not None]
            if priced:
                best = min(priced, key=lambda i: parse_price(i["price"]["value"]))
                return ebay_result(best, "ebay:api-variantes")
        if data["_status"] == 404 or 11001 in codes:
            raise ScrapeError("Article eBay introuvable : l'annonce est peut-être terminée")
        msg = "; ".join(err.get("message", "") for err in data["_errors"]) or f"HTTP {data['_status']}"
        raise ScrapeError(f"API eBay : {msg}")
    return ebay_result(data, "ebay:api")


def ebay_result(item, method):
    # Enchère : on suit l'enchère en cours ; sinon le prix « Achat immédiat ».
    current = (item.get("currentBidPrice") or {}).get("value")
    auction_only = item.get("buyingOptions") == ["AUCTION"]
    price = parse_price(current if auction_only and current else (item.get("price") or {}).get("value"))
    if price is None:
        raise ScrapeError("API eBay : pas de prix pour cet article")
    return {"title": item.get("title"), "price": price, "method": method,
            "image": (item.get("image") or {}).get("imageUrl"), "via": "api"}


# --- Extraction -----------------------------------------------------------

def parse_price(value):
    """'1 299,99 €' / '1,299.99' / 1299.99 -> 1299.99"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^\d,.\-]", "", str(value).replace(" ", "").replace("\xa0", ""))
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def iter_jsonld(page):
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))


def from_jsonld(page):
    for node in iter_jsonld(page):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" not in types:
            continue
        offers = node.get("offers")
        offers = offers if isinstance(offers, list) else [offers]
        for o in offers:
            if isinstance(o, dict):
                price = parse_price(o.get("price") or o.get("lowPrice"))
                if price is not None:
                    return node.get("name"), price, "json-ld"
    return None


def meta(page, prop):
    p = re.escape(prop)
    m = (re.search(r'<meta[^>]+(?:property|name|itemprop)="%s"[^>]+content="([^"]*)"' % p, page, re.I)
         or re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name|itemprop)="%s"' % p, page, re.I))
    return html.unescape(m.group(1)) if m else None


def from_meta(page):
    price = parse_price(meta(page, "product:price:amount") or meta(page, "og:price:amount"))
    return (meta(page, "og:title"), price, "meta") if price is not None else None


def page_title(page):
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    return html.unescape(m.group(1)).strip() if m else None


def extract_amazon(page):
    m = re.search(r'id="productTitle"[^>]*>(.*?)</span>', page, re.S)
    title = html.unescape(m.group(1)).strip() if m else None
    for pattern, label in [
        (r'id="corePrice(?:Display_desktop)?_feature_div".*?class="a-offscreen">([^<]+)<', "amazon:corePrice"),
        (r'id="apex_desktop".*?class="a-offscreen">([^<]+)<', "amazon:apex"),
        (r'class="a-price[^"]*"[^>]*>\s*<span class="a-offscreen">([^<]+)<', "amazon:a-price"),
    ]:
        m = re.search(pattern, page, re.S)
        if m:
            price = parse_price(html.unescape(m.group(1)))
            if price is not None:
                return title, price, label
    return None


def extract_ebay(page):
    t = re.search(r'class="[^"]*x-item-title__mainTitle[^"]*"[^>]*>\s*<span[^>]*>([^<]+)<', page, re.S)
    title = html.unescape(t.group(1)).strip() if t else None
    for pattern, label in [
        (r'class="[^"]*x-price-primary[^"]*"[^>]*>(?:\s*<[^>]+>)*?\s*([^<]*\d[^<]*)<', "ebay:x-price-primary"),
        (r'itemprop="price"[^>]*content="([^"]+)"', "ebay:itemprop"),
    ]:
        m = re.search(pattern, page, re.S)
        if m:
            price = parse_price(html.unescape(m.group(1)))
            if price is not None:
                return title, price, label
    return None


def extract_leboncoin(page):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    ad = (data.get("props", {}).get("pageProps", {}) or {}).get("ad") or {}
    price = ad.get("price")
    if isinstance(price, list):
        price = price[0] if price else None
    if price is None and ad.get("price_cents"):
        price = ad["price_cents"] / 100
    price = parse_price(price)
    return (ad.get("subject"), price, "leboncoin:__NEXT_DATA__") if price is not None else None


EXTRACTORS = {"amazon": extract_amazon, "ebay": extract_ebay, "leboncoin": extract_leboncoin}


def extract(site, page):
    """Extracteur propre au site, puis JSON-LD, puis balises meta."""
    for fn in [EXTRACTORS.get(site), from_jsonld, from_meta]:
        if fn is None:
            continue
        found = fn(page)
        if found:
            title, price, method = found
            return {"title": title or page_title(page), "price": price, "method": method,
                    "image": meta(page, "og:image")}
    return None


def is_blocked(status, page):
    low = page[:20000].lower()
    # Amazon répond 202 avec une page vide quand il refuse une requête.
    return status in (202, 403, 429, 503) or any(mark in low for mark in BLOCK_MARKERS)


def check(url, flaresolverr=None, ebay_keys=None):
    """Lit le prix d'une URL. Renvoie {title, price, method, image, via} ou lève ScrapeError.

    ebay_keys : (client_id, client_secret) du compte développeur eBay.
    """
    site = site_of(url)
    if site is None:
        raise ScrapeError("Site non pris en charge (Amazon, eBay ou Leboncoin uniquement)")
    if site == "ebay":
        if not ebay_keys or not all(ebay_keys):
            raise ScrapeError("Clés de l'API eBay à renseigner dans les réglages")
        return check_ebay_api(url, *ebay_keys)
    reasons = []
    for via in ["direct", "flaresolverr"]:
        if via == "flaresolverr" and not flaresolverr:
            continue
        try:
            status, page = fetch_direct(url) if via == "direct" else fetch_flaresolverr(url, flaresolverr)
        except Exception as e:  # réseau, délai dépassé, FlareSolverr injoignable...
            reasons.append(f"{via} : {type(e).__name__} {e}")
            continue
        if status in (404, 410):
            raise ScrapeError(f"Page introuvable ({status}) : l'article a peut-être été retiré")
        found = extract(site, page)
        if found:
            return {**found, "via": via}
        reasons.append(f"{via} : page bloquée par l'anti-robot" if is_blocked(status, page)
                       else f"{via} : prix introuvable dans la page (HTTP {status})")
    if site == "amazon" and flaresolverr:
        # La fiche produit est souvent bloquée, mais la page de résultats passe via
        # FlareSolverr : on cherche la référence (ASIN) et on lit le prix du résultat.
        asin = re.search(r"/dp/([A-Z0-9]{10})", url)
        if asin:
            try:
                for r in search_amazon(asin.group(1), flaresolverr):
                    if r["url"].endswith(asin.group(1)) and r["price"] is not None:
                        return {"title": r["title"], "price": r["price"], "method": "amazon:recherche-asin",
                                "image": r["image"], "via": "flaresolverr"}
                reasons.append("recherche de la référence : article absent des résultats ou sans prix")
            except ScrapeError as e:
                reasons.append(f"recherche de la référence : {e}")
    raise ScrapeError(" ; ".join(reasons))


# --- Recherche sur les sites -----------------------------------------------

def normalize(text):
    """Minuscules sans accents ni ponctuation : « iPhone 13 – 128 Go » -> « iphone 13 128 go »."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def matches(title, query, exclude=(), strict=True):
    """Résultat pertinent : contient tous les mots de la recherche et aucun mot exclu."""
    words = normalize(title).split()
    joined = " ".join(words)
    if strict:
        for w in normalize(query).split():
            # « wh1000xm5 » doit aussi trouver « WH-1000XM5 » (mots collés ou séparés).
            if w not in words and w not in joined.replace(" ", ""):
                return False
    return not any(normalize(x) and normalize(x) in joined for x in exclude)


def parse_amazon_search(page):
    results = []
    for b in re.split(r'(?=<div[^>]+data-component-type="s-search-result")', page)[1:]:
        asin = re.search(r'data-asin="([A-Z0-9]{10})"', b)
        if not asin:
            continue
        title = (re.search(r'<h2[^>]*>.*?<span[^>]*>([^<]+)</span>', b, re.S)
                 or re.search(r'<h2[^>]*aria-label="([^"]+)"', b))
        price = re.search(r'class="a-price"[^>]*>\s*<span class="a-offscreen">([^<]+)<', b)
        img = re.search(r'class="s-image"[^>]*src="([^"]+)"', b)
        results.append({
            "site": "amazon",
            "title": html.unescape(title.group(1)).strip() if title else None,
            "price": parse_price(html.unescape(price.group(1))) if price else None,
            "url": f"https://www.amazon.fr/dp/{asin.group(1)}",
            "image": img.group(1) if img else None,
            "info": "Sponsorisé" if ("puis-sponsored" in b or "AdHolder" in b) else None,
        })
    return results


def search_amazon(query, flaresolverr=None):
    """Recherche Amazon. Le direct renvoie souvent une page vide : FlareSolverr d'abord s'il existe."""
    url = "https://www.amazon.fr/s?k=" + quote_plus(query)
    reasons = []
    for via in (["flaresolverr", "direct"] if flaresolverr else ["direct"]):
        try:
            status, page = fetch_flaresolverr(url, flaresolverr) if via == "flaresolverr" else fetch_direct(url)
        except Exception as e:
            reasons.append(f"{via} : {type(e).__name__} {e}")
            continue
        results = parse_amazon_search(page)
        if results:
            return results
        if not is_blocked(status, page) and "s-search-results" in page:
            return []  # vraie page de résultats, sans résultat
        reasons.append(f"{via} : page bloquée par l'anti-robot" if is_blocked(status, page)
                       else f"{via} : aucun résultat lisible (HTTP {status})")
    if not flaresolverr:
        reasons.append("configurez FlareSolverr dans les réglages")
    raise ScrapeError(" ; ".join(reasons))


def parse_leboncoin_search(page):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return None
    try:
        props = json.loads(m.group(1)).get("props", {}).get("pageProps", {}) or {}
    except json.JSONDecodeError:
        return None
    ads = (props.get("searchData") or {}).get("ads")
    if ads is None:
        stack = [props]
        while stack and ads is None:
            node = stack.pop()
            if isinstance(node, list) and node and isinstance(node[0], dict) and "list_id" in node[0]:
                ads = node
            elif isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    if ads is None:
        return None
    results = []
    for ad in ads:
        price = ad.get("price")
        price = price[0] if isinstance(price, list) and price else price
        images = ad.get("images") or {}
        results.append({
            "site": "leboncoin",
            "title": ad.get("subject"),
            "price": parse_price(price),
            "url": ad.get("url") or f"https://www.leboncoin.fr/ad/x/{ad.get('list_id')}",
            "image": images.get("thumb_url") or (images.get("urls") or [None])[0],
            "info": (ad.get("location") or {}).get("city"),
        })
    return results


def search_leboncoin(query, flaresolverr=None):
    url = "https://www.leboncoin.fr/recherche?text=" + quote_plus(query)
    reasons = []
    for via in ["direct", "flaresolverr"]:
        if via == "flaresolverr" and not flaresolverr:
            continue
        try:
            status, page = fetch_direct(url) if via == "direct" else fetch_flaresolverr(url, flaresolverr)
        except Exception as e:
            reasons.append(f"{via} : {type(e).__name__} {e}")
            continue
        results = parse_leboncoin_search(page)
        if results is not None:
            return results
        reasons.append(f"{via} : page bloquée par l'anti-robot" if is_blocked(status, page)
                       else f"{via} : structure de page inattendue (HTTP {status})")
    raise ScrapeError(" ; ".join(reasons))


def search_ebay(query, client_id, client_secret):
    if not (client_id and client_secret):
        raise ScrapeError("Clés de l'API eBay à renseigner dans les réglages")
    token = ebay_token(client_id, client_secret)
    params = urlencode({"q": query, "limit": 50, "filter": "buyingOptions:{FIXED_PRICE}"})
    data = ebay_get(f"/buy/browse/v1/item_summary/search?{params}", token, "EBAY_FR")
    if data.get("_status"):
        msg = "; ".join(err.get("message", "") for err in data["_errors"]) or f"HTTP {data['_status']}"
        raise ScrapeError(f"API eBay : {msg}")
    return [{
        "site": "ebay",
        "title": it.get("title"),
        "price": parse_price((it.get("price") or {}).get("value")),
        "url": it.get("itemWebUrl", "").split("?")[0],
        "image": (it.get("image") or {}).get("imageUrl"),
        "info": it.get("condition"),
    } for it in data.get("itemSummaries") or []]


def search(query, sites=("amazon", "leboncoin", "ebay"), flaresolverr=None, ebay_keys=None,
           min_price=None, max_price=None, exclude=(), strict=True):
    """Lance la recherche sur chaque site en parallèle.

    Renvoie {site: {"results": [...], "hidden": n} ou {"error": "..."}} ; les résultats
    sont filtrés (mots, prix) et triés par prix croissant.
    """
    def run(site):
        if site == "amazon":
            return search_amazon(query, flaresolverr)
        if site == "leboncoin":
            return search_leboncoin(query, flaresolverr)
        return search_ebay(query, *(ebay_keys or (None, None)))

    out = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {site: pool.submit(run, site) for site in sites if site in SITES}
        for site, fut in futures.items():
            try:
                raw = fut.result()
            except ScrapeError as e:
                out[site] = {"error": str(e)}
                continue
            except Exception as e:
                out[site] = {"error": f"Erreur inattendue : {type(e).__name__} {e}"}
                continue
            kept = [r for r in raw if r["price"] is not None and matches(r["title"], query, exclude, strict)
                    and (min_price is None or r["price"] >= min_price)
                    and (max_price is None or r["price"] <= max_price)]
            kept.sort(key=lambda r: r["price"])
            out[site] = {"results": kept, "hidden": len(raw) - len(kept)}
    return out
