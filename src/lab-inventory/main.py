"""Inventaire de composants électroniques : API + interface web."""
import ipaddress
import json
import mimetypes
import os
import socket
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
PHOTOS_DIR = DATA_DIR / "photos"
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "inventaire.db"
STATIC_DIR = Path(__file__).parent / "static"

FIELDS = ["name", "part_number", "manufacturer", "category", "package", "description",
          "quantity", "location", "datasheet_url", "manufacturer_url", "notes", "photo", "photos", "specs"]
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
MAX_PHOTO = 5 * 1024 * 1024


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


with db() as conn:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS components (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        part_number TEXT, manufacturer TEXT, category TEXT, package TEXT, description TEXT,
        quantity INTEGER NOT NULL DEFAULT 1,
        location TEXT, datasheet_url TEXT, manufacturer_url TEXT, notes TEXT, photo TEXT,
        specs TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS connectors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        api_key TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)
    # Ancienne version : une seule clé Claude dans les réglages -> devient un connecteur.
    old_key = conn.execute("SELECT value FROM settings WHERE key='anthropic_api_key'").fetchone()
    if old_key and old_key["value"]:
        conn.execute("INSERT INTO connectors (provider, model, api_key) VALUES ('claude', ?, ?)",
                     (ai.PROVIDERS["claude"]["default_model"], old_key["value"]))
    conn.execute("DELETE FROM settings WHERE key='anthropic_api_key'")
    # Ancienne version : une seule photo par composant -> liste de photos (la première est la principale).
    if "photos" not in [r["name"] for r in conn.execute("PRAGMA table_info(components)")]:
        conn.execute("ALTER TABLE components ADD COLUMN photos TEXT NOT NULL DEFAULT '[]'")
        conn.execute("UPDATE components SET photos = json_array(photo) WHERE COALESCE(photo, '') != ''")


def row_to_dict(row):
    d = dict(row)
    d["specs"] = json.loads(d.get("specs") or "{}")
    d["photos"] = json.loads(d.get("photos") or "[]")
    return d


def referenced_photos():
    with db() as conn:
        return {p for r in conn.execute("SELECT photos FROM components") for p in json.loads(r["photos"] or "[]")}


def delete_photos(names):
    for name in names:
        (PHOTOS_DIR / Path(name).name).unlink(missing_ok=True)


def cleanup_orphan_photos(max_age=24 * 3600):
    """Supprime les photos jamais rattachées à un composant (ajout annulé, propositions non retenues)."""
    used, now = referenced_photos(), time.time()
    delete_photos([f.name for f in PHOTOS_DIR.iterdir()
                   if f.is_file() and f.name not in used and now - f.stat().st_mtime > max_age])


cleanup_orphan_photos()


def save_photo(data: bytes, media_type: str) -> str:
    filename = f"{uuid.uuid4().hex}.{IMAGE_TYPES.get(media_type, 'jpg')}"
    (PHOTOS_DIR / filename).write_bytes(data)
    return filename


def load_photo(name: str) -> tuple[bytes, str]:
    path = PHOTOS_DIR / Path(name).name
    if not path.is_file():
        raise HTTPException(404, f"Photo introuvable : {name}")
    ext = path.suffix.lstrip(".").replace("jpg", "jpeg")
    return path.read_bytes(), f"image/{ext}"


def mask_key(key):
    return key[:6] + "…" + key[-4:] if len(key) > 12 else "…" + key[-2:]


def connector_to_dict(row):
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["api_key"] = mask_key(d["api_key"])
    d["provider_label"] = ai.PROVIDERS.get(d["provider"], {}).get("label", d["provider"])
    return d


def connector_label(c):
    return ai.PROVIDERS.get(c["provider"], {}).get("label", c["provider"])


def active_connectors():
    """Connecteurs utilisés pour l'identification, dans l'ordre (le suivant sert de secours)."""
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM connectors WHERE enabled=1 ORDER BY id")]
    if not rows and os.environ.get("ANTHROPIC_API_KEY"):
        rows = [{"provider": "claude", "model": ai.PROVIDERS["claude"]["default_model"],
                 "api_key": os.environ["ANTHROPIC_API_KEY"]}]
    return rows


class Component(BaseModel):
    name: str
    part_number: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    package: str | None = None
    description: str | None = None
    quantity: int = 1
    location: str | None = None
    datasheet_url: str | None = None
    manufacturer_url: str | None = None
    notes: str | None = None
    photos: list[str] = []
    specs: dict = {}

    def db_values(self):
        photos = [Path(p).name for p in self.photos]
        values = {**self.model_dump(), "photos": json.dumps(photos),
                  "photo": photos[0] if photos else None,
                  "specs": json.dumps(self.specs, ensure_ascii=False)}
        return [values[f] for f in FIELDS]


class IdentifyIn(BaseModel):
    photos: list[str]


class ImageSearchIn(BaseModel):
    name: str | None = None
    part_number: str | None = None
    manufacturer: str | None = None
    package: str | None = None
    category: str | None = None


class Settings(BaseModel):
    low_stock_threshold: int | None = None


class ConnectorIn(BaseModel):
    provider: str
    model: str | None = None
    api_key: str


class ConnectorPatch(BaseModel):
    enabled: bool


DEFAULT_LOW_STOCK = 2


def get_setting(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_low_stock_threshold():
    try:
        return int(get_setting("low_stock_threshold", DEFAULT_LOW_STOCK))
    except ValueError:
        return DEFAULT_LOW_STOCK


app = FastAPI(title="Lab Inventory")


@app.get("/api/settings")
def read_settings():
    return {"ai_ready": bool(active_connectors()),
            "api_key_from_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "low_stock_threshold": get_low_stock_threshold()}


@app.put("/api/settings")
def write_settings(s: Settings):
    with db() as conn:
        if s.low_stock_threshold is not None:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('low_stock_threshold', ?)",
                         (str(max(0, s.low_stock_threshold)),))
    return read_settings()


@app.get("/api/providers")
def list_providers():
    return [{"id": k, **v} for k, v in ai.PROVIDERS.items()]


@app.get("/api/connectors")
def list_connectors():
    with db() as conn:
        return [connector_to_dict(r) for r in conn.execute("SELECT * FROM connectors ORDER BY id")]


@app.post("/api/connectors")
def create_connector(c: ConnectorIn):
    if c.provider not in ai.PROVIDERS:
        raise HTTPException(400, "Fournisseur d'IA inconnu.")
    if not c.api_key.strip():
        raise HTTPException(400, "Clé API manquante.")
    model = (c.model or "").strip() or ai.PROVIDERS[c.provider]["default_model"]
    with db() as conn:
        conn.execute("INSERT INTO connectors (provider, model, api_key) VALUES (?, ?, ?)",
                     (c.provider, model, c.api_key.strip()))
    return list_connectors()


@app.patch("/api/connectors/{cid}")
def update_connector(cid: int, p: ConnectorPatch):
    with db() as conn:
        if not conn.execute("UPDATE connectors SET enabled=? WHERE id=?", (int(p.enabled), cid)).rowcount:
            raise HTTPException(404, "Connecteur introuvable")
    return list_connectors()


@app.delete("/api/connectors/{cid}")
def delete_connector(cid: int):
    with db() as conn:
        conn.execute("DELETE FROM connectors WHERE id=?", (cid,))
    return list_connectors()


@app.get("/api/stats")
def stats():
    """Chiffres du tableau de bord."""
    threshold = get_low_stock_threshold()
    with db() as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS refs, COALESCE(SUM(quantity), 0) AS pieces, "
            "COUNT(DISTINCT NULLIF(location, '')) AS locations, "
            "SUM(CASE WHEN COALESCE(datasheet_url, '') = '' THEN 1 ELSE 0 END) AS no_datasheet "
            "FROM components").fetchone()
        by_category = conn.execute(
            "SELECT COALESCE(NULLIF(category, ''), 'Sans catégorie') AS category, "
            "COUNT(*) AS refs, SUM(quantity) AS pieces FROM components "
            "GROUP BY 1 ORDER BY refs DESC, category").fetchall()
        low_stock = conn.execute(
            "SELECT * FROM components WHERE quantity <= ? ORDER BY quantity, name LIMIT 10",
            (threshold,)).fetchall()
        low_stock_count = conn.execute(
            "SELECT COUNT(*) FROM components WHERE quantity <= ?", (threshold,)).fetchone()[0]
        recent = conn.execute(
            "SELECT * FROM components ORDER BY created_at DESC, id DESC LIMIT 5").fetchall()
    return {**dict(totals),
            "no_datasheet": totals["no_datasheet"] or 0,
            "low_stock_threshold": threshold,
            "low_stock_count": low_stock_count,
            "by_category": [dict(r) for r in by_category],
            "low_stock": [row_to_dict(r) for r in low_stock],
            "recent": [row_to_dict(r) for r in recent]}


@app.post("/api/photos")
async def upload_photos(photos: list[UploadFile] = File(...)):
    """Enregistre une ou plusieurs photos ; elles seront rattachées au composant à l'enregistrement."""
    names = []
    for photo in photos:
        data = await photo.read()
        if len(data) > MAX_PHOTO:
            raise HTTPException(413, "Photo trop lourde (5 Mo max).")
        names.append(save_photo(data, photo.content_type))
    return {"photos": names}


@app.post("/api/identify")
async def identify(body: IdentifyIn):
    """Renvoie une fiche pré-remplie par l'IA à partir des photos déjà envoyées (non encore sauvegardée)."""
    connectors = active_connectors()
    if not connectors:
        raise HTTPException(400, "Aucune IA configurée : ajoutez un connecteur dans les Paramètres.")
    if not body.photos:
        raise HTTPException(400, "Aucune photo.")
    images = [load_photo(p) for p in body.photos[:5]]
    errors = []
    for c in connectors:  # en cas d'échec, on essaie le connecteur actif suivant
        try:
            result = await run_in_threadpool(ai.identify, images, c["provider"], c["model"], c["api_key"])
        except Exception as e:
            errors.append(f"{connector_label(c)} : {e}")
            continue
        result["ai_provider"] = f"{connector_label(c)} ({c['model']})"
        return result
    return {"error": " / ".join(errors)}


def is_public_url(url: str) -> bool:
    """Refuse les adresses locales : les URL viennent du web, elles ne doivent pas viser le réseau de la maison."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    try:
        addrs = {i[4][0] for i in socket.getaddrinfo(u.hostname, None)}
    except OSError:
        return False
    return all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


def download_image(url: str):
    """Télécharge une image proposée ; renvoie le nom du fichier enregistré, ou None si ce n'est pas une image valable."""
    try:
        with httpx.Client(timeout=15, headers={"User-Agent": "Mozilla/5.0 (Lab Inventory)"}) as client:
            for _ in range(4):  # redirections suivies à la main pour vérifier chaque adresse
                if not is_public_url(url):
                    return None
                with client.stream("GET", url) as r:
                    if r.is_redirect:
                        url = urljoin(url, r.headers.get("location", ""))
                        continue
                    media_type = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if r.status_code != 200 or media_type not in IMAGE_TYPES:
                        return None
                    data = b""
                    for chunk in r.iter_bytes():
                        data += chunk
                        if len(data) > MAX_PHOTO:
                            return None
                    if len(data) < 3000:  # icônes, pixels de suivi…
                        return None
                    return save_photo(data, media_type)
    except httpx.HTTPError:
        return None
    return None


@app.post("/api/image-suggestions")
async def image_suggestions(body: ImageSearchIn):
    """Cherche des photos du composant sur le web (via une IA avec recherche) et les enregistre comme propositions."""
    desc = "\n".join(f"- {label} : {value}" for label, value in [
        ("Nom", body.name), ("Référence", body.part_number), ("Fabricant", body.manufacturer),
        ("Boîtier", body.package), ("Catégorie", body.category)] if value)
    if not desc:
        raise HTTPException(400, "Indiquez au moins le nom ou la référence du composant.")
    connectors = [c for c in active_connectors() if ai.PROVIDERS.get(c["provider"], {}).get("web_search")]
    if not connectors:
        raise HTTPException(400, "Aucune IA avec recherche web active (Claude, ChatGPT ou Gemini).")
    errors = []
    for c in connectors:
        try:
            found = await run_in_threadpool(ai.find_images, desc, c["provider"], c["model"], c["api_key"])
        except Exception as e:
            errors.append(f"{connector_label(c)} : {e}")
            continue
        urls = list(dict.fromkeys(i["url"] for i in found))[:10]
        pages = {i["url"]: i.get("page") for i in found}
        with ThreadPoolExecutor(max_workers=6) as pool:
            files = await run_in_threadpool(lambda: list(pool.map(download_image, urls)))
        images = [{"photo": f, "url": u, "page": pages.get(u)} for u, f in zip(urls, files) if f]
        if images:
            return {"images": images, "ai_provider": connector_label(c)}
        errors.append(f"{connector_label(c)} : aucune image exploitable trouvée")
    raise HTTPException(502, " / ".join(errors))


@app.get("/api/components")
def list_components(q: str = "", category: str = "", low_stock: bool = False):
    sql = "SELECT * FROM components WHERE 1=1"
    args = []
    if q:
        sql += " AND (name LIKE ? OR part_number LIKE ? OR manufacturer LIKE ? OR description LIKE ? OR location LIKE ?)"
        args += [f"%{q}%"] * 5
    if category == "Sans catégorie":
        sql += " AND COALESCE(category, '') = ''"
    elif category:
        sql += " AND category = ?"
        args.append(category)
    if low_stock:
        sql += " AND quantity <= ?"
        args.append(get_low_stock_threshold())
    sql += " ORDER BY updated_at DESC"
    with db() as conn:
        return [row_to_dict(r) for r in conn.execute(sql, args)]


@app.get("/api/components/{cid}")
def get_component(cid: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM components WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Composant introuvable")
    return row_to_dict(row)


@app.post("/api/components")
def create_component(c: Component):
    with db() as conn:
        cur = conn.execute(
            f"INSERT INTO components ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
            c.db_values())
        cid = cur.lastrowid
    return get_component(cid)


@app.put("/api/components/{cid}")
def update_component(cid: int, c: Component):
    old_photos = get_component(cid)["photos"]
    with db() as conn:
        conn.execute(
            f"UPDATE components SET {', '.join(f + '=?' for f in FIELDS)}, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?", c.db_values() + [cid])
    delete_photos(set(old_photos) - referenced_photos())  # photos retirées de la fiche
    return get_component(cid)


class QuantityPatch(BaseModel):
    quantity: int


@app.patch("/api/components/{cid}")
def update_quantity(cid: int, p: QuantityPatch):
    """Ajustement rapide du stock depuis la fiche en lecture."""
    with db() as conn:
        if not conn.execute("UPDATE components SET quantity=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (max(0, p.quantity), cid)).rowcount:
            raise HTTPException(404, "Composant introuvable")
    return get_component(cid)


@app.delete("/api/components/{cid}")
def delete_component(cid: int):
    photos = get_component(cid)["photos"]
    with db() as conn:
        conn.execute("DELETE FROM components WHERE id=?", (cid,))
    delete_photos(set(photos) - referenced_photos())
    return {"ok": True}


@app.get("/photos/{name}")
def get_photo(name: str):
    path = PHOTOS_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


mimetypes.add_type("font/woff2", ".woff2")


class Static(StaticFiles):
    """Fichiers de l'interface ; la page est toujours revalidée pour qu'une mise à jour de l'app soit vue tout de suite."""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in ("", ".", "index.html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", Static(directory=STATIC_DIR, html=True), name="static")
