"""Inventaire de composants électroniques : API + interface web."""
import json
import os
import sqlite3
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
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
    """)


def row_to_dict(row):
    d = dict(row)
    d["specs"] = json.loads(d.get("specs") or "{}")
    return d


def get_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='anthropic_api_key'").fetchone()
    return row["value"] if row else None


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
    anthropic_api_key: str


app = FastAPI(title="Inventaire composants")


@app.get("/api/settings")
def read_settings():
    return {"api_key_set": bool(get_api_key()),
            "api_key_from_env": bool(os.environ.get("ANTHROPIC_API_KEY"))}


@app.put("/api/settings")
def write_settings(s: Settings):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('anthropic_api_key', ?)",
                     (s.anthropic_api_key.strip(),))
    return read_settings()


@app.post("/api/identify")
async def identify(photo: UploadFile = File(...)):
    """Enregistre la photo et renvoie une fiche pré-remplie par l'IA (non encore sauvegardée)."""
    key = get_api_key()
    if not key:
        raise HTTPException(400, "Clé API Claude manquante : renseignez-la dans les réglages.")
    data = await photo.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "Photo trop lourde (5 Mo max).")
    media_type = photo.content_type if photo.content_type in (
        "image/jpeg", "image/png", "image/webp", "image/gif") else "image/jpeg"
    filename = f"{uuid.uuid4().hex}.{media_type.split('/')[1]}"
    (PHOTOS_DIR / filename).write_bytes(data)
    try:
        result = ai.identify(data, media_type, key)
    except Exception as e:  # on renvoie quand même la photo pour une saisie manuelle
        return {"photo": filename, "error": str(e)}
    result["photo"] = filename
    return result


@app.get("/api/components")
def list_components(q: str = "", category: str = ""):
    sql = "SELECT * FROM components WHERE 1=1"
    args = []
    if q:
        sql += " AND (name LIKE ? OR part_number LIKE ? OR manufacturer LIKE ? OR description LIKE ? OR location LIKE ?)"
        args += [f"%{q}%"] * 5
    if category:
        sql += " AND category = ?"
        args.append(category)
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
