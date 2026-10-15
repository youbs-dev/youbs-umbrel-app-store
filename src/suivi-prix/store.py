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
    # Adresses Apprise, une par ligne (ntfy://, tgram://, mailto://, discord://...).
    "apprise_urls": os.environ.get("APPRISE_URLS", ""),
    # Prix « anormalement bas » : X % sous la médiane des N derniers jours,
    # dès qu'il y a assez de relevés pour que la médiane ait un sens.
    "anomaly_pct": "20",
    "anomaly_days": "30",
    "anomaly_min_history": "5",
}
# Jamais renvoyés au navigateur (les adresses Apprise contiennent des jetons).
SECRET_SETTINGS = {"ebay_client_secret", "apprise_urls"}


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
            threshold_alerted REAL,
            anomaly_alerted REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            price REAL NOT NULL,
            checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS prices_item ON prices(item_id, checked_at);
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            price REAL NOT NULL,
            reference REAL,
            ok INTEGER NOT NULL,
            error TEXT,
            sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS alerts_item ON alerts(item_id, sent_at);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        -- Résultats écartés à la main d'une recherche (fausse correspondance, annonce trompeuse) :
        -- ils n'apparaissent plus dans cette recherche ni dans la veille du même nom.
        CREATE TABLE IF NOT EXISTS ignored (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_key TEXT NOT NULL,
            url TEXT NOT NULL,
            site TEXT,
            title TEXT,
            price REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(query_key, url)
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # 0.2.0 : un « article » peut aussi être une veille sur une recherche (kind='search').
        # Son prix est alors le meilleur prix trouvé, best_url/best_site disant où.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        for col, decl in [("threshold_alerted", "REAL"), ("anomaly_alerted", "REAL"),
                          ("kind", "TEXT NOT NULL DEFAULT 'item'"), ("query", "TEXT"), ("sites", "TEXT"),
                          ("min_price", "REAL"), ("max_price", "REAL"), ("exclude", "TEXT"),
                          ("best_url", "TEXT"), ("best_site", "TEXT"),
                          # 0.3.0 : articles de sites différents regroupés sous un même produit.
                          ("product_id", "INTEGER REFERENCES products(id) ON DELETE SET NULL")]:
            if col not in existing:
                conn.execute(f"ALTER TABLE items ADD COLUMN {col} {decl}")
        # 0.4.0 : offre d'où vient chaque relevé d'une veille, pour effacer ceux d'une offre écartée.
        if "url" not in {r["name"] for r in conn.execute("PRAGMA table_info(prices)")}:
            conn.execute("ALTER TABLE prices ADD COLUMN url TEXT")


def get_settings():
    with db() as conn:
        stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    return {**DEFAULT_SETTINGS, **stored}


def set_settings(values):
    with db() as conn:
        conn.executemany("INSERT INTO settings(key, value) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         [(k, str(v)) for k, v in values.items() if k in DEFAULT_SETTINGS])
