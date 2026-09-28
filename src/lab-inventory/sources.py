"""Sources gratuites de données et d'images, sans IA : API Mouser et Wikimedia Commons."""
import httpx

UA = "LabInventory (+https://github.com/youbs-dev/youbs-umbrel-app-store)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def mouser_search(keyword: str, api_key: str, records: int = 5) -> list[dict]:
    """Recherche par mot-clé ou référence dans le catalogue Mouser (clé API gratuite)."""
    r = httpx.post("https://api.mouser.com/api/v1/search/keyword", params={"apiKey": api_key},
                   json={"SearchByKeywordRequest": {"keyword": keyword, "records": records, "startingRecord": 0}},
                   headers={"User-Agent": UA}, timeout=TIMEOUT)
    if r.is_error:
        raise RuntimeError(f"HTTP {r.status_code}")
    data = r.json()
    if data.get("Errors"):
        raise RuntimeError(data["Errors"][0].get("Message") or "erreur inconnue")
    return (data.get("SearchResults") or {}).get("Parts") or []


def mouser_best_part(part_number: str | None, name: str | None, api_key: str) -> dict | None:
    """Le produit Mouser qui correspond le mieux (référence exacte en priorité)."""
    keyword = (part_number or name or "").strip()
    if not keyword:
        return None
    parts = mouser_search(keyword, api_key, records=10)
    exact = [p for p in parts if (p.get("ManufacturerPartNumber") or "").upper() == keyword.upper()]
    return (exact or parts or [None])[0]


def mouser_images(part_number: str | None, name: str | None, api_key: str) -> list[dict]:
    keyword = (part_number or name or "").strip()
    images, seen = [], set()
    for p in mouser_search(keyword, api_key, records=10):
        url = p.get("ImagePath")
        if url and url not in seen:
            seen.add(url)
            images.append({"url": url, "page": p.get("ProductDetailUrl"),
                           "title": " · ".join(filter(None, [p.get("ManufacturerPartNumber"), p.get("Manufacturer")]))})
    return images


def wikimedia_images(query: str, limit: int = 8) -> list[dict]:
    """Photos libres de Wikimedia Commons (sans clé), redimensionnées à 800 px."""
    r = httpx.get("https://commons.wikimedia.org/w/api.php", headers={"User-Agent": UA}, timeout=TIMEOUT, params={
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": 6, "gsrlimit": limit, "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": 800})
    r.raise_for_status()
    pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    images = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") in ("image/jpeg", "image/png", "image/webp") and info.get("thumburl"):
            images.append({"url": info["thumburl"], "page": info.get("descriptionurl"),
                           "title": p.get("title", "").removeprefix("File:")})
    return images
