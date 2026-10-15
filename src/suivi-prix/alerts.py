"""Alertes de prix : passage sous le seuil, ou prix anormalement bas par rapport à l'habitude.

Chaque règle ne se déclenche qu'une fois par baisse : elle se réarme quand le prix
remonte au-dessus de sa limite, et relance une alerte si le prix baisse encore.
"""
import logging
import statistics
from datetime import datetime, timedelta, timezone

import apprise

from store import db, get_settings

log = logging.getLogger("suivi-prix")


def eur(v):
    return f"{v:,.2f} €".replace(",", " ").replace(".", ",")


def send(title, body, urls=None):
    """Envoie une notification à toutes les adresses Apprise configurées. Renvoie (ok, erreur)."""
    urls = urls if urls is not None else get_settings()["apprise_urls"]
    targets = [u.strip() for u in urls.replace(",", "\n").splitlines() if u.strip()]
    if not targets:
        return False, "Aucune adresse de notification configurée"
    ap = apprise.Apprise()
    invalid = [u.split("://")[0] + "://…" for u in targets if not ap.add(u)]
    if invalid:
        return False, "Adresse non reconnue : " + ", ".join(invalid)
    return (True, None) if ap.notify(title=title, body=body) else (False, "L'envoi a échoué (voir le journal de l'app)")


def usual_price(conn, item_id, days, exclude_last=True):
    """Médiane des relevés des `days` derniers jours, hors relevé qui vient d'arriver."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [r["price"] for r in conn.execute(
        "SELECT price FROM prices WHERE item_id=? AND checked_at>=? ORDER BY checked_at, id", (item_id, since))]
    if exclude_last and rows:
        rows = rows[:-1]
    return (statistics.median(rows), len(rows)) if rows else (None, 0)


def evaluate(item_id, price):
    """Appelée après chaque relevé réussi : décide des alertes à envoyer et les envoie."""
    s = get_settings()
    pct = float(s["anomaly_pct"] or 0)
    days = int(float(s["anomaly_days"] or 30))
    min_history = int(float(s["anomaly_min_history"] or 5))
    to_send = []
    with db() as conn:
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        name = item["title"] or item["url"]

        # 1. Seuil fixé par l'utilisateur.
        threshold, alerted = item["threshold"], item["threshold_alerted"]
        if threshold is not None and price <= threshold:
            if alerted is None or price < alerted:
                to_send.append(("seuil", threshold,
                                f"{name} est à {eur(price)}, sous votre seuil de {eur(threshold)}."))
                conn.execute("UPDATE items SET threshold_alerted=? WHERE id=?", (price, item_id))
        elif alerted is not None:
            conn.execute("UPDATE items SET threshold_alerted=NULL WHERE id=?", (item_id,))

        # 2. Prix anormalement bas par rapport à la médiane récente.
        usual, count = usual_price(conn, item_id, days)
        alerted = item["anomaly_alerted"]
        if pct > 0 and usual and count >= min_history and price <= usual * (1 - pct / 100):
            if alerted is None or price < alerted:
                drop = (1 - price / usual) * 100
                to_send.append(("anomalie", usual,
                                f"{name} est à {eur(price)}, soit {drop:.0f} % sous son prix habituel "
                                f"({eur(usual)} sur {days} jours)."))
                conn.execute("UPDATE items SET anomaly_alerted=? WHERE id=?", (price, item_id))
        elif alerted is not None and (usual is None or price > usual * (1 - pct / 100)):
            conn.execute("UPDATE items SET anomaly_alerted=NULL WHERE id=?", (item_id,))

    for kind, reference, body in to_send:
        title = "Prix sous le seuil" if kind == "seuil" else "Prix anormalement bas"
        ok, error = send(title, f"{body}\n{item['url']}")
        with db() as conn:
            conn.execute("INSERT INTO alerts(item_id, kind, price, reference, ok, error) VALUES(?, ?, ?, ?, ?, ?)",
                         (item_id, kind, price, reference, int(ok), error))
        log.info("Alerte %s pour l'article %s : %s", kind, item_id, "envoyée" if ok else error)
