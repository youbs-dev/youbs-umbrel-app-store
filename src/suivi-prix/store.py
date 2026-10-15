"""Base SQLite : catégories, articles suivis, historique des prix et réglages."""
import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "suivi-prix.db"
STATIC_DIR = Path(__file__).parent / "static"

DEFAULT_SETTINGS = {
    "interval_hours": "6",
    "flaresolverr_url": os.environ.get("FLARESOLVERR_URL", ""),
    "ebay_client_id": "",
    "ebay_client_secret": "",
}
SECRET_SETTINGS = {"ebay_client_secret"}


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE
        );
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            site TEXT NOT NULL,
            title TEXT,
            image TEXT,
            category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            threshold REAL,
            active INTEGER NOT NULL DEFAULT 1,
            last_price REAL,
            last_checked TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            price REAL NOT NULL,
            checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS prices_item ON prices(item_id, checked_at);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)


def get_settings():
    with db() as conn:
        stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    return {**DEFAULT_SETTINGS, **stored}


def set_settings(values):
    with db() as conn:
        conn.executemany("INSERT INTO settings(key, value) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         [(k, str(v)) for k, v in values.items() if k in DEFAULT_SETTINGS])
