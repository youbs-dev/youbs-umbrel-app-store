"""Inventaire de composants électroniques : API + interface web."""
import json
import os
import sqlite3
import uuid
from pathlib import Path

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
          "quantity", "location", "datasheet_url", "manufacturer_url", "notes", "photo"]


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


def row_to_dict(row):
    d = dict(row)
    d["specs"] = json.loads(d.get("specs") or "{}")
    return d


def mask_key(key):
    return key[:6] + "…" + key[-4:] if len(key) > 12 else "…" + key[-2:]


def connector_to_dict(row):
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["api_key"] = mask_key(d["api_key"])
    d["provider_label"] = ai.PROVIDERS.get(d["provider"], {}).get("label", d["provider"])
    return d


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
    photo: str | None = None
    specs: dict = {}


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


@app.post("/api/identify")
async def identify(photo: UploadFile = File(...)):
    """Enregistre la photo et renvoie une fiche pré-remplie par l'IA (non encore sauvegardée)."""
    connectors = active_connectors()
    if not connectors:
        raise HTTPException(400, "Aucune IA configurée : ajoutez un connecteur dans les Paramètres.")
    data = await photo.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "Photo trop lourde (5 Mo max).")
    media_type = photo.content_type if photo.content_type in (
        "image/jpeg", "image/png", "image/webp", "image/gif") else "image/jpeg"
    filename = f"{uuid.uuid4().hex}.{media_type.split('/')[1]}"
    (PHOTOS_DIR / filename).write_bytes(data)
    errors = []
    for c in connectors:  # en cas d'échec, on essaie le connecteur actif suivant
        label = ai.PROVIDERS.get(c["provider"], {}).get("label", c["provider"])
        try:
            result = await run_in_threadpool(ai.identify, data, media_type, c["provider"], c["model"], c["api_key"])
        except Exception as e:
            errors.append(f"{label} : {e}")
            continue
        result["photo"] = filename
        result["ai_provider"] = f"{label} ({c['model']})"
        return result
    # on renvoie quand même la photo pour une saisie manuelle
    return {"photo": filename, "error": " / ".join(errors)}


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
    values = [getattr(c, f) for f in FIELDS] + [json.dumps(c.specs, ensure_ascii=False)]
    with db() as conn:
        cur = conn.execute(
            f"INSERT INTO components ({', '.join(FIELDS)}, specs) VALUES ({', '.join('?' * (len(FIELDS) + 1))})",
            values)
        cid = cur.lastrowid
    return get_component(cid)


@app.put("/api/components/{cid}")
def update_component(cid: int, c: Component):
    get_component(cid)
    values = [getattr(c, f) for f in FIELDS] + [json.dumps(c.specs, ensure_ascii=False), cid]
    with db() as conn:
        conn.execute(
            f"UPDATE components SET {', '.join(f + '=?' for f in FIELDS)}, specs=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?", values)
    return get_component(cid)


@app.delete("/api/components/{cid}")
def delete_component(cid: int):
    with db() as conn:
        conn.execute("DELETE FROM components WHERE id=?", (cid,))
    return {"ok": True}


@app.get("/photos/{name}")
def get_photo(name: str):
    path = PHOTOS_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
