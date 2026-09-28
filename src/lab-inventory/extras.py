"""Fonctions de gestion : emplacements, doublons, étiquettes QR, historique, projets, courses,
export / sauvegarde et datasheets hors ligne."""
import csv
import io
import json
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
import qrcode
import qrcode.image.svg
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from starlette.background import BackgroundTask

import sources
from store import (DATA_DIR, DATASHEETS_DIR, DB_PATH, PHOTOS_DIR, clean_name, db, get_low_stock_threshold,
                   get_setting, is_public_url, log_movement, migrate, row_to_dict)

router = APIRouter()


def component_or_404(conn, cid):
    row = conn.execute("SELECT * FROM components WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "Composant introuvable")
    return row_to_dict(row)


# ---------------------------------------------------------------- Emplacements

class LocationIn(BaseModel):
    name: str
    description: str | None = None


def clean_location(name):
    name = clean_name(name)
    if not name:
        raise HTTPException(400, "Le nom de l'emplacement est vide.")
    if name.lower() == "sans emplacement":
        raise HTTPException(400, "« Sans emplacement » est réservé aux composants non rangés.")
    return name


@router.get("/api/locations")
def list_locations():
    """Emplacements avec le nombre de références et de pièces rangées dedans."""
    with db() as conn:
        rows = conn.execute(
            "SELECT l.id, l.name, l.description, COUNT(c.id) AS count, COALESCE(SUM(c.quantity), 0) AS pieces "
            "FROM locations l LEFT JOIN components c ON c.location = l.name COLLATE NOCASE "
            "GROUP BY l.id ORDER BY l.name COLLATE NOCASE").fetchall()
        unplaced = conn.execute("SELECT COUNT(*) FROM components WHERE COALESCE(location, '') = ''").fetchone()[0]
    return {"locations": [dict(r) for r in rows], "unplaced": unplaced}


@router.post("/api/locations")
def create_location(body: LocationIn):
    name = clean_location(body.name)
    with db() as conn:
        try:
            conn.execute("INSERT INTO locations (name, description) VALUES (?, ?)", (name, body.description))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"L'emplacement « {name} » existe déjà.")
    return list_locations()


@router.put("/api/locations/{loc_id}")
def update_location(loc_id: int, body: LocationIn):
    """Renomme l'emplacement (et ses composants) ou change sa description."""
    name = clean_location(body.name)
    with db() as conn:
        row = conn.execute("SELECT name FROM locations WHERE id=?", (loc_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Emplacement introuvable")
        try:
            conn.execute("UPDATE locations SET name=?, description=? WHERE id=?", (name, body.description, loc_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"L'emplacement « {name} » existe déjà.")
        conn.execute("UPDATE components SET location=? WHERE location=? COLLATE NOCASE", (name, row["name"]))
    return list_locations()


@router.delete("/api/locations/{loc_id}")
def delete_location(loc_id: int):
    """Supprime l'emplacement ; ses composants passent en « Sans emplacement »."""
    with db() as conn:
        row = conn.execute("SELECT name FROM locations WHERE id=?", (loc_id,)).fetchone()
        if row:
            conn.execute("UPDATE components SET location=NULL WHERE location=? COLLATE NOCASE", (row["name"],))
            conn.execute("DELETE FROM locations WHERE id=?", (loc_id,))
    return list_locations()


# ---------------------------------------------------------------- Doublons

@router.get("/api/duplicates")
def find_duplicates(part_number: str = "", name: str = ""):
    """Composants déjà en stock avec la même référence (ou, à défaut de référence, le même nom)."""
    part_number, name = clean_name(part_number), clean_name(name)
    with db() as conn:
        if part_number:
            rows = conn.execute("SELECT * FROM components WHERE TRIM(part_number) = ? COLLATE NOCASE",
                                (part_number,)).fetchall()
        elif name:
            rows = conn.execute("SELECT * FROM components WHERE TRIM(name) = ? COLLATE NOCASE", (name,)).fetchall()
        else:
            rows = []
    return [row_to_dict(r) for r in rows]


class MergeIn(BaseModel):
    quantity: int
    photos: list[str] = []


@router.post("/api/components/{cid}/merge")
def merge_into(cid: int, body: MergeIn):
    """Ajoute les pièces (et les photos) d'un nouvel ajout à une fiche existante au lieu de créer un doublon."""
    with db() as conn:
        c = component_or_404(conn, cid)
        qty = c["quantity"] + max(0, body.quantity)
        photos = c["photos"] + [p for p in body.photos if p not in c["photos"]]
        conn.execute("UPDATE components SET quantity=?, photos=?, photo=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (qty, json.dumps(photos), photos[0] if photos else None, cid))
        log_movement(conn, cid, qty - c["quantity"], qty, "Ajout (même référence)")
        return component_or_404(conn, cid)


# ---------------------------------------------------------------- Étiquettes QR

@router.get("/api/qr.svg")
def qr_svg(data: str):
    if len(data) > 1000:
        raise HTTPException(400, "Texte trop long pour un QR code.")
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=1,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    svg = img.to_string().decode()
    svg = re.sub(r'\s(width|height)="[^"]*"', "", svg, count=2)  # taille libre, fixée par la page d'étiquettes
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------- Historique

@router.get("/api/components/{cid}/movements")
def component_movements(cid: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT m.*, p.name AS project_name FROM movements m LEFT JOIN projects p ON p.id = m.project_id "
            "WHERE m.component_id=? ORDER BY m.id DESC LIMIT 100", (cid,)).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/movements")
def recent_movements(limit: int = 50):
    with db() as conn:
        rows = conn.execute(
            "SELECT m.*, c.name AS component_name, c.part_number, p.name AS project_name FROM movements m "
            "LEFT JOIN components c ON c.id = m.component_id LEFT JOIN projects p ON p.id = m.project_id "
            "ORDER BY m.id DESC LIMIT ?", (min(limit, 500),)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- Projets et nomenclature

class ProjectIn(BaseModel):
    name: str
    description: str | None = None
    builds: int = 1


class ItemIn(BaseModel):
    component_id: int | None = None
    designators: str | None = None
    value: str | None = None
    footprint: str | None = None
    part_number: str | None = None
    qty_per_build: int = 1


class ConsumeIn(BaseModel):
    builds: int
    force: bool = False


def match_component(conn, part_number, value):
    """Retrouve le composant du stock qui correspond à une ligne de nomenclature."""
    for sql, arg in (("TRIM(part_number) = ? COLLATE NOCASE", part_number),
                     ("TRIM(part_number) = ? COLLATE NOCASE", value),
                     ("TRIM(name) = ? COLLATE NOCASE", value)):
        arg = clean_name(arg)
        if arg:
            row = conn.execute(f"SELECT id FROM components WHERE {sql} ORDER BY quantity DESC LIMIT 1", (arg,)).fetchone()
            if row:
                return row["id"]
    return None


def project_detail(conn, pid):
    p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "Projet introuvable")
    p = dict(p)
    items = conn.execute(
        "SELECT i.*, c.name AS component_name, c.part_number AS component_part_number, c.quantity AS stock, "
        "c.location AS component_location, c.photo AS component_photo "
        "FROM project_items i LEFT JOIN components c ON c.id = i.component_id "
        "WHERE i.project_id=? ORDER BY i.id", (pid,)).fetchall()
    p["items"], counts = [], {"ok": 0, "short": 0, "missing": 0}
    for r in items:
        it = dict(r)
        it["needed"] = it["qty_per_build"] * p["builds"]
        stock = it["stock"] if it["component_id"] else 0
        it["status"] = "missing" if not it["component_id"] or not stock else ("ok" if stock >= it["needed"] else "short")
        it["to_buy"] = max(0, it["needed"] - (stock or 0))
        counts[it["status"]] += 1
        p["items"].append(it)
    p["counts"] = counts
    return p


@router.get("/api/projects")
def list_projects():
    with db() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM projects ORDER BY updated_at DESC, id DESC")]
        out = []
        for pid in ids:
            p = project_detail(conn, pid)
            p["item_count"] = len(p.pop("items"))
            out.append(p)
    return out


@router.post("/api/projects")
def create_project(body: ProjectIn):
    name = clean_name(body.name)
    if not name:
        raise HTTPException(400, "Le nom du projet est vide.")
    with db() as conn:
        pid = conn.execute("INSERT INTO projects (name, description, builds) VALUES (?, ?, ?)",
                           (name, body.description, max(1, body.builds))).lastrowid
        return project_detail(conn, pid)


@router.get("/api/projects/{pid}")
def get_project(pid: int):
    with db() as conn:
        return project_detail(conn, pid)


@router.put("/api/projects/{pid}")
def update_project(pid: int, body: ProjectIn):
    name = clean_name(body.name)
    if not name:
        raise HTTPException(400, "Le nom du projet est vide.")
    with db() as conn:
        if not conn.execute("UPDATE projects SET name=?, description=?, builds=?, updated_at=CURRENT_TIMESTAMP "
                            "WHERE id=?", (name, body.description, max(1, body.builds), pid)).rowcount:
            raise HTTPException(404, "Projet introuvable")
        return project_detail(conn, pid)


@router.delete("/api/projects/{pid}")
def delete_project(pid: int):
    with db() as conn:
        conn.execute("DELETE FROM project_items WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    return {"ok": True}


@router.post("/api/projects/{pid}/items")
def add_item(pid: int, body: ItemIn):
    with db() as conn:
        project_detail(conn, pid)
        cid = body.component_id or match_component(conn, body.part_number, body.value)
        conn.execute("INSERT INTO project_items (project_id, component_id, designators, value, footprint, part_number, "
                     "qty_per_build) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (pid, cid, body.designators, body.value, body.footprint, body.part_number,
                      max(1, body.qty_per_build)))
        conn.execute("UPDATE projects SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (pid,))
        return project_detail(conn, pid)


@router.put("/api/projects/{pid}/items/{item_id}")
def update_item(pid: int, item_id: int, body: ItemIn):
    with db() as conn:
        if not conn.execute("UPDATE project_items SET component_id=?, designators=?, value=?, footprint=?, "
                            "part_number=?, qty_per_build=? WHERE id=? AND project_id=?",
                            (body.component_id, body.designators, body.value, body.footprint, body.part_number,
                             max(1, body.qty_per_build), item_id, pid)).rowcount:
            raise HTTPException(404, "Ligne introuvable")
        return project_detail(conn, pid)


@router.delete("/api/projects/{pid}/items/{item_id}")
def delete_item(pid: int, item_id: int):
    with db() as conn:
        conn.execute("DELETE FROM project_items WHERE id=? AND project_id=?", (item_id, pid))
        return project_detail(conn, pid)


HEADERS = {
    "designators": ("reference", "references", "ref", "refs", "designator", "designators", "référence", "repère",
                    "repères"),
    "value": ("value", "valeur", "val", "comment"),
    "footprint": ("footprint", "package", "boîtier", "boitier", "empreinte"),
    "qty": ("qty", "quantity", "quantité", "quantite", "qté", "count", "nb"),
    "part_number": ("mpn", "manufacturer part number", "manufacturer_part_number", "part number", "partnumber",
                    "mfr part", "mfr. part #", "mfr part number", "référence fabricant", "lcsc", "lcsc part"),
}


def parse_bom(text: str) -> list[dict]:
    """Lit une nomenclature CSV (export KiCad, JLCPCB, tableur…) : repères, valeur, empreinte, quantité, MPN."""
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        raise HTTPException(400, "Fichier vide.")
    header = [h.strip().strip('"').lower() for h in rows[0]]
    cols = {key: next((i for i, h in enumerate(header) if h in names), None) for key, names in HEADERS.items()}
    if cols["designators"] is None and cols["value"] is None and cols["part_number"] is None:
        raise HTTPException(400, "Colonnes non reconnues : il faut au moins « Reference », « Value » ou « MPN ».")

    def cell(row, key):
        i = cols[key]
        return row[i].strip() if i is not None and i < len(row) else ""

    grouped = {}
    for row in rows[1:]:
        if not any(x.strip() for x in row):
            continue
        des, value, fp, pn = cell(row, "designators"), cell(row, "value"), cell(row, "footprint"), cell(row, "part_number")
        if not (des or value or pn):
            continue
        refs = [r for r in re.split(r"[,;\s]+", des) if r]
        qty_text = cell(row, "qty")
        qty = int(qty_text) if qty_text.isdigit() else max(1, len(refs))
        key = (value.lower(), fp.lower(), pn.lower())
        g = grouped.setdefault(key, {"designators": [], "value": value, "footprint": fp, "part_number": pn, "qty": 0})
        g["designators"] += refs
        g["qty"] += qty
    return list(grouped.values())


@router.post("/api/projects/{pid}/import")
async def import_bom(pid: int, file: UploadFile = File(...), replace: bool = False):
    raw = await file.read()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = parse_bom(text)
    with db() as conn:
        project_detail(conn, pid)
        if replace:
            conn.execute("DELETE FROM project_items WHERE project_id=?", (pid,))
        for ln in lines:
            conn.execute("INSERT INTO project_items (project_id, component_id, designators, value, footprint, "
                         "part_number, qty_per_build) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (pid, match_component(conn, ln["part_number"], ln["value"]), ", ".join(ln["designators"]),
                          ln["value"] or None, ln["footprint"] or None, ln["part_number"] or None, max(1, ln["qty"])))
        conn.execute("UPDATE projects SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (pid,))
        p = project_detail(conn, pid)
    p["imported"] = len(lines)
    return p


@router.post("/api/projects/{pid}/consume")
def consume(pid: int, body: ConsumeIn):
    """Retire du stock les pièces utilisées pour fabriquer le projet (et le note dans l'historique)."""
    builds = max(1, body.builds)
    with db() as conn:
        p = project_detail(conn, pid)
        linked = [it for it in p["items"] if it["component_id"]]
        short = [it for it in linked if it["stock"] < it["qty_per_build"] * builds]
        if short and not body.force:
            raise HTTPException(409, "Stock insuffisant pour : " + ", ".join(
                f"{it['component_name']} ({it['stock']}/{it['qty_per_build'] * builds})" for it in short))
        used = 0
        reason = f"Projet « {p['name']} » ({builds} ex.)"
        for it in linked:
            take = min(it["stock"], it["qty_per_build"] * builds)
            if take:
                conn.execute("UPDATE components SET quantity = quantity - ?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (take, it["component_id"]))
                after = conn.execute("SELECT quantity FROM components WHERE id=?", (it["component_id"],)).fetchone()[0]
                log_movement(conn, it["component_id"], -take, after, reason, pid)
                used += take
        p = project_detail(conn, pid)
    p["consumed"] = used
    return p


# ---------------------------------------------------------------- Liste de courses

@router.get("/api/shopping")
def shopping_list():
    """Composants en stock bas + ce qui manque pour fabriquer les projets."""
    threshold = get_low_stock_threshold()
    entries = {}

    def entry(key, **info):
        return entries.setdefault(key, {"key": key, "reasons": [], "to_buy": 0, **info})

    with db() as conn:
        for r in conn.execute("SELECT * FROM components WHERE quantity <= ? ORDER BY name", (threshold,)):
            e = entry(f"c{r['id']}", component_id=r["id"], name=r["name"], part_number=r["part_number"],
                      manufacturer=r["manufacturer"], location=r["location"], in_stock=r["quantity"], needed=0)
            e["reasons"].append(f"Stock bas ({r['quantity']} ≤ {threshold})")
        for pid in [r["id"] for r in conn.execute("SELECT id FROM projects")]:
            p = project_detail(conn, pid)
            for it in p["items"]:
                if it["component_id"]:
                    if it["status"] == "ok" and f"c{it['component_id']}" not in entries:
                        continue
                    e = entry(f"c{it['component_id']}", component_id=it["component_id"], name=it["component_name"],
                              part_number=it["component_part_number"], manufacturer=None,
                              location=it["component_location"], in_stock=it["stock"], needed=0)
                else:
                    label = it["part_number"] or it["value"] or it["designators"]
                    e = entry(f"p:{(it['part_number'] or it['value'] or '').lower()}|{(it['footprint'] or '').lower()}",
                              component_id=None, name=label, part_number=it["part_number"], manufacturer=None,
                              location=None, in_stock=0, footprint=it["footprint"], needed=0)
                e["reasons"].append(f"Projet « {p['name']} » : {it['needed']} nécessaire(s)")
                e["needed"] += it["needed"]
        for e in entries.values():
            # de quoi fabriquer les projets et rester au-dessus du seuil de stock bas ensuite
            target = e["needed"] + (threshold + 1 if e["component_id"] else 0)
            e["to_buy"] = max(0, target - e["in_stock"])
    items = [e for e in entries.values() if e["to_buy"]]
    return {"items": sorted(items, key=lambda e: (e["name"] or "").lower()), "threshold": threshold}


class PriceIn(BaseModel):
    items: list[dict]


def unit_price(part, qty):
    """Prix unitaire Mouser pour la quantité demandée (palier de prix le plus proche en dessous)."""
    best = None
    for pb in part.get("PriceBreaks") or []:
        if pb.get("Quantity", 0) <= max(qty, 1) and (best is None or pb["Quantity"] >= best["Quantity"]):
            best = pb
    best = best or (part.get("PriceBreaks") or [None])[0]
    return (best.get("Price"), best.get("Currency")) if best else (None, None)


@router.post("/api/shopping/prices")
def shopping_prices(body: PriceIn):
    """Prix et disponibilité Mouser pour les lignes de la liste de courses (clé API Mouser nécessaire)."""
    key = get_setting("mouser_api_key")
    if not key:
        raise HTTPException(400, "Ajoutez une clé API Mouser dans les Paramètres pour voir les prix.")
    out = {}
    for it in body.items[:30]:
        ref = clean_name(it.get("part_number") or it.get("name"))
        if not ref:
            continue
        try:
            part = sources.mouser_best_part(ref, None, key)
        except Exception as e:
            out[it["key"]] = {"error": str(e)}
            continue
        if not part:
            out[it["key"]] = {"error": "introuvable chez Mouser"}
            continue
        price, currency = unit_price(part, int(it.get("to_buy") or 1))
        out[it["key"]] = {"price": price, "currency": currency, "availability": part.get("Availability"),
                          "url": part.get("ProductDetailUrl"), "mpn": part.get("ManufacturerPartNumber"),
                          "manufacturer": part.get("Manufacturer")}
        time.sleep(0.3)  # l'API Mouser limite le nombre d'appels par minute
    return out


# ---------------------------------------------------------------- Export et sauvegarde

CSV_COLUMNS = [("id", "ID"), ("name", "Nom"), ("part_number", "Référence"), ("manufacturer", "Fabricant"),
               ("category", "Catégorie"), ("package", "Boîtier"), ("quantity", "Quantité"),
               ("location", "Emplacement"), ("description", "Description"), ("datasheet_url", "Datasheet"),
               ("manufacturer_url", "Page fabricant"), ("specs", "Caractéristiques"), ("notes", "Notes"),
               ("created_at", "Ajouté le"), ("updated_at", "Modifié le")]


@router.get("/api/export/components.csv")
def export_csv():
    """Inventaire complet en CSV (séparateur « ; » et UTF-8 avec BOM, pour Excel en français)."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([label for _, label in CSV_COLUMNS])
    with db() as conn:
        for r in conn.execute("SELECT * FROM components ORDER BY name COLLATE NOCASE"):
            d = row_to_dict(r)
            d["specs"] = " | ".join(f"{k} : {v}" for k, v in d["specs"].items())
            w.writerow(["" if d.get(k) is None else d[k] for k, _ in CSV_COLUMNS])
    name = f"lab-inventory-{datetime.now():%Y-%m-%d}.csv"
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/backup.zip")
def backup():
    """Sauvegarde complète : base de données, photos et datasheets."""
    tmp = Path(tempfile.mkdtemp())
    db_copy = tmp / "inventaire.db"
    with sqlite3.connect(DB_PATH) as src, sqlite3.connect(db_copy) as dst:
        src.backup(dst)  # copie cohérente même si l'app écrit en même temps
    zip_path = tmp / f"lab-inventory-{datetime.now():%Y-%m-%d-%H%M}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(db_copy, "inventaire.db")
        for folder in (PHOTOS_DIR, DATASHEETS_DIR):
            for f in folder.iterdir():
                if f.is_file():
                    z.write(f, f"{folder.name}/{f.name}")
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


@router.post("/api/restore")
def restore(file: UploadFile = File(...)):
    """Restaure une sauvegarde : l'ancienne base est gardée à côté (inventaire.db.avant-restauration-…)."""
    tmp = Path(tempfile.mkdtemp())
    try:
        return _restore(file, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _restore(file, tmp):
    zpath = tmp / "backup.zip"
    with zpath.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        z = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile:
        raise HTTPException(400, "Ce fichier n'est pas une sauvegarde Lab Inventory (zip illisible).")
    with z:
        if "inventaire.db" not in z.namelist():
            raise HTTPException(400, "Sauvegarde invalide : inventaire.db absent.")
        new_db = tmp / "inventaire.db"
        new_db.write_bytes(z.read("inventaire.db"))
        if not new_db.read_bytes()[:16].startswith(b"SQLite format 3"):
            raise HTTPException(400, "Sauvegarde invalide : la base de données est illisible.")
        shutil.copy2(DB_PATH, DATA_DIR / f"inventaire.db.avant-restauration-{datetime.now():%Y%m%d-%H%M%S}")
        with sqlite3.connect(new_db) as src, sqlite3.connect(DB_PATH) as dst:
            src.backup(dst)
        restored = 0
        for name in z.namelist():
            folder, _, fname = name.partition("/")
            target = {"photos": PHOTOS_DIR, "datasheets": DATASHEETS_DIR}.get(folder)
            fname = Path(fname).name  # jamais de chemin : pas d'écriture hors des dossiers de l'app
            if target and fname:
                (target / fname).write_bytes(z.read(name))
                restored += 1
    migrate()  # une sauvegarde plus ancienne reçoit les nouvelles tables
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
    return {"components": count, "files": restored}


# ---------------------------------------------------------------- Datasheets hors ligne

MAX_PDF = 40 * 1024 * 1024


def download_pdf(url: str):
    """Télécharge un PDF (redirections vérifiées une à une) ; renvoie son contenu ou lève une erreur lisible."""
    with httpx.Client(timeout=30, headers={"User-Agent": sources.UA, "Accept": "application/pdf,*/*"}) as client:
        for _ in range(5):
            if not is_public_url(url):
                raise RuntimeError("adresse refusée")
            with client.stream("GET", url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                data = b""
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_PDF:
                        raise RuntimeError("fichier trop lourd")
                if not data.startswith(b"%PDF"):
                    raise RuntimeError("le lien ne mène pas directement à un PDF")
                return data
    raise RuntimeError("trop de redirections")


def save_datasheet(cid):
    with db() as conn:
        c = component_or_404(conn, cid)
    if not c.get("datasheet_url"):
        raise RuntimeError("pas de lien de datasheet dans la fiche")
    data = download_pdf(c["datasheet_url"])
    name = f"{cid}-{uuid.uuid4().hex[:8]}.pdf"
    (DATASHEETS_DIR / name).write_bytes(data)
    with db() as conn:
        conn.execute("UPDATE components SET datasheet_file=? WHERE id=?", (name, cid))
    if c.get("datasheet_file"):
        (DATASHEETS_DIR / Path(c["datasheet_file"]).name).unlink(missing_ok=True)
    return name


@router.post("/api/components/{cid}/datasheet")
async def download_datasheet(cid: int):
    try:
        await run_in_threadpool(save_datasheet, cid)
    except RuntimeError as e:
        raise HTTPException(502, f"Datasheet non récupérée : {e}")
    with db() as conn:
        return component_or_404(conn, cid)


@router.delete("/api/components/{cid}/datasheet")
def remove_datasheet(cid: int):
    with db() as conn:
        c = component_or_404(conn, cid)
        conn.execute("UPDATE components SET datasheet_file=NULL WHERE id=?", (cid,))
    if c.get("datasheet_file"):
        (DATASHEETS_DIR / Path(c["datasheet_file"]).name).unlink(missing_ok=True)
    with db() as conn:
        return component_or_404(conn, cid)


@router.post("/api/datasheets/download-all")
async def download_all_datasheets():
    """Copie localement toutes les datasheets qui ne le sont pas encore."""
    with db() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM components WHERE COALESCE(datasheet_url, '') != '' AND COALESCE(datasheet_file, '') = ''")]
    ok, failed = 0, []
    for cid in ids:
        try:
            await run_in_threadpool(save_datasheet, cid)
            ok += 1
        except Exception as e:
            failed.append({"id": cid, "error": str(e)})
    return {"downloaded": ok, "failed": failed}


@router.get("/datasheets/{name}")
def get_datasheet(name: str):
    path = DATASHEETS_DIR / Path(name).name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf", content_disposition_type="inline")
