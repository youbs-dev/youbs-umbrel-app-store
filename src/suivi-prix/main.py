"""Suivi de prix : API, relevé périodique et interface web."""
import asyncio
import logging
import json
import statistics
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import alerts
import scraper
from store import SECRET_SETTINGS, STATIC_DIR, db, get_settings, migrate, set_settings

log = logging.getLogger("suivi-prix")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Pause entre deux articles lors d'un relevé, pour ne pas solliciter les sites en rafale.
PAUSE_BETWEEN_CHECKS = 5
HISTORY_DAYS = 30


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --- Relevé des prix -------------------------------------------------------

check_lock = asyncio.Lock()


def scrape(url):
    s = get_settings()
    return scraper.check(url, flaresolverr=s["flaresolverr_url"] or None,
                         ebay_keys=(s["ebay_client_id"], s["ebay_client_secret"]))


def split_words(text):
    return [w.strip() for w in (text or "").replace(";", ",").split(",") if w.strip()]


def run_search(query, sites, min_price=None, max_price=None, exclude="", strict=True):
    s = get_settings()
    return scraper.search(query, sites=sites, flaresolverr=s["flaresolverr_url"] or None,
                          ebay_keys=(s["ebay_client_id"], s["ebay_client_secret"]),
                          min_price=min_price, max_price=max_price, exclude=split_words(exclude), strict=strict)


def scrape_watch(row):
    """Relevé d'une veille : relance la recherche et retient la meilleure offre."""
    sites = json.loads(row["sites"] or "[]") or list(scraper.SITES)
    out = run_search(row["query"], sites, row["min_price"], row["max_price"], row["exclude"])
    offers = [r for site in out.values() for r in site.get("results", [])]
    errors = [f"{scraper.SITES[k]} : {v['error']}" for k, v in out.items() if "error" in v]
    if not offers:
        raise scraper.ScrapeError(" ; ".join(errors) or "Aucune offre ne correspond à la recherche et aux filtres")
    best = min(offers, key=lambda r: r["price"])
    # Erreurs partielles (un site bloqué) : signalées sans empêcher le relevé.
    return {"title": None, "price": best["price"], "image": best["image"], "method": f"veille:{best['site']}",
            "best_url": best["url"], "best_site": best["site"], "best_title": best["title"],
            "warning": " ; ".join(errors) or None}


def record(item_id, result=None, error=None):
    with db() as conn:
        if result:
            conn.execute("INSERT INTO prices(item_id, price, checked_at) VALUES(?, ?, ?)",
                         (item_id, result["price"], now()))
            conn.execute("UPDATE items SET last_price=?, last_checked=?, last_error=?, "
                         "title=COALESCE(?, title), image=COALESCE(?, image), "
                         "best_url=COALESCE(?, best_url), best_site=COALESCE(?, best_site) WHERE id=?",
                         (result["price"], now(), result.get("warning"), result.get("title"), result.get("image"),
                          result.get("best_url"), result.get("best_site"), item_id))
        else:
            conn.execute("UPDATE items SET last_checked=?, last_error=? WHERE id=?", (now(), error, item_id))


async def check_item(item_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return
    try:
        if row["kind"] == "search":
            result = await run_in_threadpool(scrape_watch, row)
        else:
            result = await run_in_threadpool(scrape, row["url"])
        record(item_id, result=result)
        log.info("Article %s : %.2f € (%s)", item_id, result["price"], result["method"])
        await run_in_threadpool(alerts.evaluate, item_id, result["price"])
    except scraper.ScrapeError as e:
        record(item_id, error=str(e))
        log.warning("Article %s : %s", item_id, e)
    except Exception as e:  # ne jamais arrêter la boucle de relevé
        record(item_id, error=f"Erreur inattendue : {type(e).__name__} {e}")
        log.exception("Article %s", item_id)


def due_items():
    hours = float(get_settings()["interval_hours"] or 6)
    limit = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM items WHERE active=1 AND (last_checked IS NULL OR last_checked < ?) "
            "ORDER BY last_checked IS NOT NULL, last_checked", (limit,))]


async def scheduler():
    while True:
        try:
            for item_id in due_items():
                async with check_lock:
                    await check_item(item_id)
                await asyncio.sleep(PAUSE_BETWEEN_CHECKS)
        except Exception:
            log.exception("Boucle de relevé")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_app):
    migrate()
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()


app = FastAPI(title="Suivi de prix", lifespan=lifespan)


# --- Articles --------------------------------------------------------------

def item_to_dict(row, prices):
    d = dict(row)
    d["active"] = bool(d["active"])
    d["below_threshold"] = d.pop("threshold_alerted") is not None
    d["abnormally_low"] = d.pop("anomaly_alerted") is not None
    d["site_label"] = "Veille" if d["kind"] == "search" else scraper.SITES.get(d["site"], d["site"])
    d["sites"] = json.loads(d["sites"]) if d["sites"] else None
    d["best_site_label"] = scraper.SITES.get(d["best_site"]) if d["best_site"] else None
    values = [p["price"] for p in prices]
    since = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    recent = [p["price"] for p in prices if p["checked_at"] >= since]
    d["stats"] = {
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "median_30d": statistics.median(recent) if recent else None,
        "previous": next((v for v in reversed(values[:-1]) if v != values[-1]), None) if values else None,
        "count": len(values),
    }
    return d


def load_items(where="", params=()):
    with db() as conn:
        rows = conn.execute(
            "SELECT items.*, categories.name AS category FROM items "
            "LEFT JOIN categories ON categories.id = items.category_id " + where +
            " ORDER BY items.created_at DESC", params).fetchall()
        out = []
        for r in rows:
            prices = conn.execute("SELECT price, checked_at FROM prices WHERE item_id=? ORDER BY checked_at",
                                  (r["id"],)).fetchall()
            out.append(item_to_dict(r, prices))
    return out


def get_item(item_id):
    items = load_items("WHERE items.id=?", (item_id,))
    if not items:
        raise HTTPException(404, "Article introuvable")
    return items[0]


class ItemIn(BaseModel):
    url: str
    category_id: int | None = None
    threshold: float | None = None


class ItemPatch(BaseModel):
    category_id: int | None = None
    threshold: float | None = None
    active: bool | None = None
    title: str | None = None
    # Veilles uniquement
    min_price: float | None = None
    max_price: float | None = None
    exclude: str | None = None
    sites: list[str] | None = None


class SearchIn(BaseModel):
    query: str
    sites: list[str] = list(scraper.SITES)
    min_price: float | None = None
    max_price: float | None = None
    exclude: str = ""
    strict: bool = True


class WatchIn(SearchIn):
    category_id: int | None = None
    threshold: float | None = None


def clean_sites(sites):
    sites = [s for s in sites if s in scraper.SITES]
    if not sites:
        raise HTTPException(400, "Choisissez au moins un site")
    return sites


@app.post("/api/search")
async def search(body: SearchIn):
    """Recherche ponctuelle sur les sites choisis (résultats filtrés et triés par prix)."""
    query = body.query.strip()
    if len(query) < 2:
        raise HTTPException(400, "Recherche trop courte")
    return await run_in_threadpool(run_search, query, clean_sites(body.sites), body.min_price,
                                   body.max_price, body.exclude, body.strict)


@app.post("/api/watches", status_code=201)
async def add_watch(body: WatchIn):
    """Enregistre une recherche comme veille : elle est relancée à chaque relevé."""
    query = body.query.strip()
    if len(query) < 2:
        raise HTTPException(400, "Recherche trop courte")
    with db() as conn:
        item_id = conn.execute(
            "INSERT INTO items(url, site, kind, title, query, sites, min_price, max_price, exclude, category_id, threshold) "
            "VALUES(?, 'multi', 'search', ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"search:{uuid.uuid4()}", query, query, json.dumps(clean_sites(body.sites)), body.min_price,
             body.max_price, body.exclude.strip() or None, body.category_id, body.threshold)).lastrowid
    async with check_lock:
        await check_item(item_id)
    return get_item(item_id)


@app.get("/api/items")
def list_items():
    return load_items()


@app.post("/api/items", status_code=201)
async def add_item(body: ItemIn):
    url = scraper.clean_url(body.url)
    site = scraper.site_of(url)
    if not url.startswith(("http://", "https://")) or site is None:
        raise HTTPException(400, "Collez le lien d'un article Amazon, eBay ou Leboncoin")
    with db() as conn:
        if conn.execute("SELECT 1 FROM items WHERE url=?", (url,)).fetchone():
            raise HTTPException(409, "Cet article est déjà suivi")
        item_id = conn.execute("INSERT INTO items(url, site, category_id, threshold) VALUES(?, ?, ?, ?)",
                               (url, site, body.category_id, body.threshold)).lastrowid
    async with check_lock:
        await check_item(item_id)
    return get_item(item_id)


@app.get("/api/items/{item_id}")
def read_item(item_id: int):
    return get_item(item_id)


@app.patch("/api/items/{item_id}")
def update_item(item_id: int, body: ItemPatch):
    get_item(item_id)
    changes = body.model_dump(exclude_unset=True)
    if changes:
        if "active" in changes:
            changes["active"] = int(changes["active"])
        if "threshold" in changes:
            # Nouveau seuil : la règle se réarme et sera réévaluée au prochain relevé.
            changes["threshold_alerted"] = None
        if "sites" in changes:
            changes["sites"] = json.dumps(clean_sites(changes["sites"] or []))
        cols = ", ".join(f"{k}=?" for k in changes)
        with db() as conn:
            conn.execute(f"UPDATE items SET {cols} WHERE id=?", (*changes.values(), item_id))
    return get_item(item_id)


@app.delete("/api/items/{item_id}", status_code=204)
def delete_item(item_id: int):
    with db() as conn:
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))


@app.post("/api/items/{item_id}/check")
async def check_now(item_id: int):
    get_item(item_id)
    async with check_lock:
        await check_item(item_id)
    return get_item(item_id)


@app.get("/api/items/{item_id}/prices")
def item_prices(item_id: int):
    get_item(item_id)
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT price, checked_at FROM prices WHERE item_id=? ORDER BY checked_at", (item_id,))]


@app.get("/api/items/{item_id}/alerts")
def item_alerts(item_id: int):
    get_item(item_id)
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT kind, price, reference, ok, error, sent_at FROM alerts WHERE item_id=? "
            "ORDER BY sent_at DESC, id DESC LIMIT 20", (item_id,))]


# --- Catégories ------------------------------------------------------------

class CategoryIn(BaseModel):
    name: str


@app.get("/api/categories")
def list_categories():
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT categories.id, categories.name, COUNT(items.id) AS count FROM categories "
            "LEFT JOIN items ON items.category_id = categories.id GROUP BY categories.id ORDER BY categories.name")]


@app.post("/api/categories", status_code=201)
def add_category(body: CategoryIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Nom de catégorie vide")
    with db() as conn:
        existing = conn.execute("SELECT id, name FROM categories WHERE name=?", (name,)).fetchone()
        if existing:
            return dict(existing)
        cat_id = conn.execute("INSERT INTO categories(name) VALUES(?)", (name,)).lastrowid
    return {"id": cat_id, "name": name}


@app.patch("/api/categories/{cat_id}")
def rename_category(cat_id: int, body: CategoryIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Nom de catégorie vide")
    with db() as conn:
        try:
            conn.execute("UPDATE categories SET name=? WHERE id=?", (name, cat_id))
        except Exception:
            raise HTTPException(409, "Une catégorie porte déjà ce nom")
    return {"id": cat_id, "name": name}


@app.delete("/api/categories/{cat_id}", status_code=204)
def delete_category(cat_id: int):
    with db() as conn:
        conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))


# --- Réglages --------------------------------------------------------------

@app.get("/api/settings")
def read_settings():
    s = get_settings()
    # Les secrets ne repartent jamais vers le navigateur : on indique seulement s'ils sont définis.
    return {k: (bool(v) if k in SECRET_SETTINGS else v) for k, v in s.items()}


@app.put("/api/settings")
def write_settings(values: dict):
    for key, low, high, label in [("anomaly_pct", 0, 90, "Écart en %"), ("anomaly_days", 1, 365, "Période en jours"),
                                  ("anomaly_min_history", 1, 1000, "Nombre minimal de relevés")]:
        if key in values:
            try:
                ok = low <= float(values[key]) <= high
            except (TypeError, ValueError):
                ok = False
            if not ok:
                raise HTTPException(400, f"{label} : valeur entre {low} et {high} attendue")
    if "interval_hours" in values:
        try:
            hours = float(values["interval_hours"])
        except (TypeError, ValueError):
            raise HTTPException(400, "Intervalle invalide")
        if not 0.25 <= hours <= 168:
            raise HTTPException(400, "Intervalle entre 15 minutes et 7 jours")
    # Un secret laissé vide dans le formulaire conserve la valeur enregistrée.
    values = {k: v for k, v in values.items() if not (k in SECRET_SETTINGS and v in ("", None, True))}
    set_settings(values)
    return read_settings()


class NotifyTest(BaseModel):
    apprise_urls: str | None = None


@app.post("/api/notify/test")
async def notify_test(body: NotifyTest):
    """Envoie une notification d'essai, aux adresses saisies ou à celles enregistrées."""
    urls = body.apprise_urls if body.apprise_urls and body.apprise_urls.strip() else None
    ok, error = await run_in_threadpool(alerts.send, "Suivi de prix",
                                        "Notification d'essai : les alertes de prix arriveront ici.", urls)
    if not ok:
        raise HTTPException(400, error)
    return {"ok": True}


# --- Interface -------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
