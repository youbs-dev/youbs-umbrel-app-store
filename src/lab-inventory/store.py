"""Socle commun : dossiers de données, base SQLite et ses migrations, photos, réglages."""
import ipaddress
import json
import os
import socket
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException

import ai

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
PHOTOS_DIR = DATA_DIR / "photos"
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
DATASHEETS_DIR = DATA_DIR / "datasheets"
DATASHEETS_DIR.mkdir(parents=True, exist_ok=True)
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


def migrate():
    """Crée ou met à jour le schéma (au démarrage et après une restauration de sauvegarde)."""
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
        # Catégories modifiables dans les paramètres : liste par défaut + celles déjà utilisées par des composants.
        if not conn.execute("SELECT name FROM sqlite_master WHERE name='categories'").fetchone():
            conn.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE)")
            conn.executemany("INSERT OR IGNORE INTO categories (name) VALUES (?)", [(c,) for c in ai.DEFAULT_CATEGORIES])
            conn.execute("INSERT OR IGNORE INTO categories (name) "
                         "SELECT DISTINCT TRIM(category) FROM components WHERE TRIM(COALESCE(category, '')) != ''")
        # Emplacements (tiroirs, boîtes) gérés comme les catégories : reprise de ceux déjà saisis.
        if not conn.execute("SELECT name FROM sqlite_master WHERE name='locations'").fetchone():
            conn.execute("CREATE TABLE locations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "name TEXT NOT NULL UNIQUE COLLATE NOCASE, description TEXT)")
            conn.execute("INSERT OR IGNORE INTO locations (name) "
                         "SELECT DISTINCT TRIM(location) FROM components WHERE TRIM(COALESCE(location, '')) != ''")
        # Datasheet copiée localement (PDF dans data/datasheets).
        if "datasheet_file" not in [r["name"] for r in conn.execute("PRAGMA table_info(components)")]:
            conn.execute("ALTER TABLE components ADD COLUMN datasheet_file TEXT")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            component_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            quantity_after INTEGER NOT NULL,
            reason TEXT,
            project_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS movements_component ON movements(component_id);
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            builds INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS project_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            component_id INTEGER,
            designators TEXT,
            value TEXT,
            footprint TEXT,
            part_number TEXT,
            qty_per_build INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS project_items_project ON project_items(project_id);
        """)


migrate()


def log_movement(conn, component_id, delta, quantity_after, reason, project_id=None):
    """Historique du stock : chaque entrée ou sortie de pièces est notée."""
    if delta:
        conn.execute("INSERT INTO movements (component_id, delta, quantity_after, reason, project_id) "
                     "VALUES (?, ?, ?, ?, ?)", (component_id, delta, quantity_after, reason, project_id))


def clean_name(name: str) -> str:
    return " ".join((name or "").split())


def remember(conn, table, name):
    """Une catégorie ou un emplacement saisi dans une fiche rejoint la liste des paramètres."""
    name = clean_name(name)
    if name and name.lower() not in ("sans catégorie", "sans emplacement"):
        conn.execute(f"INSERT OR IGNORE INTO {table} (name) VALUES (?)", (name,))


def row_to_dict(row):
    d = dict(row)
    d["specs"] = json.loads(d.get("specs") or "{}")
    d["photos"] = json.loads(d.get("photos") or "[]")
    return d


def referenced_photos():
    with db() as conn:
        return {p for r in conn.execute("SELECT photos FROM components") for p in json.loads(r["photos"] or "[]")}


def is_remote(photo: str) -> bool:
    """Une photo est soit un fichier local, soit un lien vers une image que le serveur n'a pas pu copier (Mouser)."""
    return photo.startswith(("http://", "https://"))


def delete_photos(names):
    for name in names:
        if not is_remote(name):
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
