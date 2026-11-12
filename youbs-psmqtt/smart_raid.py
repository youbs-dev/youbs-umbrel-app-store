"""SMART des disques du boitier RAID USB Orico, publie sur MQTT pour Home Assistant.

Le boitier (pont USB JMicron JMS567 + puce RAID JMicron JMB39x) ne montre a
Linux qu'un disque virtuel. smartctl lit chaque disque physique avec le mode
"jmb39x,N", qui ecrit des commandes dans le secteur 33 du volume puis l'efface
(smartctl refuse si ce secteur n'est pas vide). Ce mode n'est utilise que si le
dossier conf/autoriser-jmb39x existe ; il est verifie a chaque cycle, sans
redemarrage.

Avant smartctl, le service lit aussi l'etat de chaque port avec la commande du
logiciel JMicron HW RAID Manager (module jmb39x.py) et journalise la reponse
brute, pour decoder l'etat du RAID.

Seul ce service doit interroger la puce RAID : deux programmes qui utilisent le
secteur 33 en meme temps melangeraient leurs commandes.

Reglages MQTT (broker, identifiant, prefixes) lus dans conf/psmqtt.yaml.
"""

import glob
import json
import os
import subprocess
import time

import paho.mqtt.client as mqtt
import yaml

import jmb39x

CONF_DIR = "/opt/psmqtt/conf"
FLAG = os.path.join(CONF_DIR, "autoriser-jmb39x")
PERIODE = int(os.environ.get("PERIODE_SEC", "600"))
TYPE_JMB = os.environ.get("TYPE_JMB", "jmb39x")
NB_DISQUES = 5  # la puce JMB39x gere jusqu'a 5 disques
MODELE_VOLUME = "USB3.0 DISK"  # modele SCSI du volume RAID vu par Linux


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def lire_conf():
    with open(os.path.join(CONF_DIR, "psmqtt.yaml"), encoding="utf-8") as f:
        conf = yaml.safe_load(f)
    m = conf["mqtt"]
    return {
        "host": m["broker"]["host"],
        "port": int(m["broker"].get("port", 1883)),
        "username": m["broker"].get("username"),
        "password": m["broker"].get("password"),
        "prefixe": m.get("publish_topic_prefix", "psmqtt/umbrel/"),
        "ha": (m.get("ha_discovery") or {}).get("topic", "homeassistant"),
        "appareil": (m.get("ha_discovery") or {}).get("device_name", "Umbrel"),
    }


def trouver_volume():
    """Retrouve /dev/sdX du boitier par son modele (le nom peut changer)."""
    for bloc in sorted(glob.glob("/sys/block/sd*")):
        try:
            with open(os.path.join(bloc, "device/model"), encoding="utf-8") as f:
                if MODELE_VOLUME in f.read():
                    return "/dev/" + os.path.basename(bloc)
        except OSError:
            continue
    return None


def lire_raids(dev):
    """Volumes RAID vus par la puce (commande de HW RAID Manager)."""
    raids = {}
    with jmb39x.Session(dev) as s:
        for n in range(NB_DISQUES):
            p = s.info_raid(n)
            r = jmb39x.decoder_raid(p)
            if r:
                raids[n] = r
                log(f"RAID {n + 1} : {r}")
                if r["etat_code"] != jmb39x.ETAT_NORMAL:
                    log(f"RAID {n + 1} brut : {p.hex()}")
    return raids


def attribut(data, ident):
    for a in (data.get("ata_smart_attributes") or {}).get("table", []):
        if a.get("id") == ident:
            return (a.get("raw") or {}).get("value")
    return None


def lire_disque(dev, n):
    res = subprocess.run(
        ["smartctl", "-j", "-x", "-d", f"{TYPE_JMB},{n}", dev],
        capture_output=True, text=True, timeout=120,
    )
    try:
        data = json.loads(res.stdout)
    except ValueError:
        log(f"disque {n} : reponse illisible de smartctl (code {res.returncode})")
        return None
    if not data.get("serial_number"):
        msgs = [m.get("string") for m in (data.get("smartctl") or {}).get("messages", [])]
        log(f"disque {n} : absent ou illisible (code {res.returncode}) {msgs}")
        return None
    return {
        "modele": data.get("model_name"),
        "serie": data.get("serial_number"),
        "firmware": data.get("firmware_version"),
        "sante": "OK" if (data.get("smart_status") or {}).get("passed") else "ECHEC",
        "temperature": (data.get("temperature") or {}).get("current"),
        "heures": (data.get("power_on_time") or {}).get("hours"),
        "cycles": data.get("power_cycle_count"),
        "realloues": attribut(data, 5),
        "en_attente": attribut(data, 197),
        "non_corrigibles": attribut(data, 198),
    }


# (cle, nom, unite, device_class, icone)
CAPTEURS = [
    ("temperature", "Température", "°C", "temperature", "mdi:thermometer"),
    ("heures", "Heures de fonctionnement", "h", "duration", "mdi:timer-outline"),
    ("realloues", "Secteurs réalloués", None, None, "mdi:harddisk-remove"),
    ("en_attente", "Secteurs en attente", None, None, "mdi:harddisk-remove"),
    ("non_corrigibles", "Secteurs non corrigibles", None, None, "mdi:harddisk-remove"),
    ("cycles", "Cycles d'alimentation", None, None, "mdi:power-cycle"),
]


def appareil(conf):
    """Appareil Home Assistant de PSMQTT (memes identifiants : "ids" = device_name)."""
    return {"identifiers": [conf["appareil"]], "name": conf["appareil"]}


def config(client, conf, composant, objet, cfg):
    client.publish(f"{conf['ha']}/{composant}/umbrel-orico/{objet}/config", json.dumps(cfg), retain=True)


def publier_disque(client, conf, n, d):
    num = n + 1
    etat = f"{conf['prefixe']}orico/disque{num}"
    base = {"state_topic": etat, "device": appareil(conf), "expire_after": PERIODE * 3}
    for cle, nom, unite, classe, icone in CAPTEURS:
        cfg = dict(base, name=f"ORICO Disque {num} {nom}", unique_id=f"umbrel-orico-disque{num}-{cle}",
                   icon=icone, value_template=f"{{{{ value_json.{cle} }}}}", state_class="measurement")
        if unite:
            cfg["unit_of_measurement"] = unite
        if classe:
            cfg["device_class"] = classe
        if cle in ("heures", "cycles"):
            cfg["state_class"] = "total_increasing"
        config(client, conf, "sensor", f"disque{num}_{cle}", cfg)
    config(client, conf, "sensor", f"disque{num}_modele", dict(
        base, name=f"ORICO Disque {num} Modèle", unique_id=f"umbrel-orico-disque{num}-modele",
        icon="mdi:harddisk", value_template="{{ value_json.modele }}",
        json_attributes_topic=etat, json_attributes_template=(
            "{{ {'serie': value_json.serie, 'firmware': value_json.firmware} | tojson }}")))
    config(client, conf, "binary_sensor", f"disque{num}_sante", dict(
        base, name=f"ORICO Disque {num} Problème SMART", unique_id=f"umbrel-orico-disque{num}-sante",
        device_class="problem", value_template="{{ 'ON' if value_json.sante != 'OK' else 'OFF' }}"))
    client.publish(etat, json.dumps(d), retain=True)


def publier_raid(client, conf, disques, raids):
    etat = f"{conf['prefixe']}orico/raid"
    base = {"state_topic": etat, "device": appareil(conf), "expire_after": PERIODE * 3}
    for cle, nom, icone in (("etat_raid", "État du RAID", "mdi:shield-check"),
                            ("niveau_raid", "Niveau RAID", "mdi:layers"),
                            ("disques_raid", "Disques dans le RAID", "mdi:harddisk-plus"),
                            ("disques", "Disques détectés", "mdi:harddisk")):
        config(client, conf, "sensor", cle, dict(
            base, name=f"ORICO {nom}", unique_id=f"umbrel-orico-{cle}", icon=icone,
            value_template=f"{{{{ value_json.{cle} }}}}"))
    config(client, conf, "binary_sensor", "raid_degrade", dict(
        base, name="ORICO RAID dégradé", unique_id="umbrel-orico-raid-degrade", device_class="problem",
        value_template="{{ 'ON' if value_json.raid_degrade else 'OFF' }}"))
    config(client, conf, "binary_sensor", "probleme", dict(
        base, name="ORICO Problème disque", unique_id="umbrel-orico-probleme", device_class="problem",
        value_template="{{ 'ON' if value_json.probleme else 'OFF' }}"))
    r = raids.get(0)
    raid_degrade = r is None or r["etat_code"] != jmb39x.ETAT_NORMAL
    probleme = raid_degrade or any(
        d["sante"] != "OK" or (d["realloues"] or 0) > 0 or (d["en_attente"] or 0) > 0
        or (d["non_corrigibles"] or 0) > 0 for d in disques.values())
    client.publish(etat, json.dumps({
        "disques": len(disques), "probleme": probleme, "raid_degrade": raid_degrade,
        "etat_raid": r["etat"] if r else "Introuvable",
        "niveau_raid": r["niveau"] if r else None,
        "disques_raid": r["nb_disques"] if r else None,
    }), retain=True)


def nettoyer_anciens(client, conf, disques):
    """Supprime de Home Assistant les appareils "Disque RAID N" et "Boitier RAID
    Orico" publies par les versions 2.6.1.2 a 2.6.1.5 (message vide retenu)."""
    vides = [f"binary_sensor/orico-raid/{o}" for o in ("probleme", "raid_ok")]
    vides += [f"sensor/orico-raid/{o}" for o in ("disques", "etat_raid", "niveau_raid", "disques_raid")]
    for d in disques.values():
        uid = f"orico-raid-{d['serie']}"
        vides += [f"sensor/{uid}/{c[0]}" for c in CAPTEURS] + [f"binary_sensor/{uid}/sante"]
    for t in vides:
        client.publish(f"{conf['ha']}/{t}/config", "", retain=True)
    for t in ["boitier"] + [f"disque{n}" for n in range(1, NB_DISQUES + 1)]:
        client.publish(f"{conf['prefixe']}raid/{t}", "", retain=True)


def connecter(conf):
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="psmqtt-umbrel-raid")
    except AttributeError:  # paho-mqtt 1.x
        client = mqtt.Client(client_id="psmqtt-umbrel-raid")
    if conf["username"]:
        client.username_pw_set(conf["username"], conf["password"])
    client.connect(conf["host"], conf["port"], keepalive=PERIODE * 2)
    client.loop_start()
    return client


def cycle():
    if not os.path.exists(FLAG):
        log("mode jmb39x non autorisé (dossier conf/autoriser-jmb39x absent) : rien n'est lu")
        return
    dev = trouver_volume()
    if not dev:
        log("boîtier RAID USB introuvable")
        return
    # Un seul dialogue a la fois avec la puce : l'etat du RAID, puis smartctl
    try:
        raids = lire_raids(dev)
    except jmb39x.SecteurNonVide as e:
        log(f"{e} : lecture jmb39x abandonnée pour ne rien écraser")
        return
    except Exception as e:
        log(f"lecture de l'état du RAID impossible : {e!r}")
        raids = {}
    disques = {}
    for n in range(NB_DISQUES):
        d = lire_disque(dev, n)
        if d:
            disques[n] = d
            log(f"disque {n + 1} : {d}")
    if not disques and not raids:
        return
    conf = lire_conf()
    client = connecter(conf)
    try:
        nettoyer_anciens(client, conf, disques)
        for n, d in disques.items():
            publier_disque(client, conf, n, d)
        publier_raid(client, conf, disques, raids)
    finally:
        time.sleep(2)
        client.loop_stop()
        client.disconnect()


def main():
    while True:
        try:
            cycle()
        except Exception as e:  # le service ne doit pas s'arreter sur une erreur passagere
            log(f"erreur : {e!r}")
        time.sleep(PERIODE)


if __name__ == "__main__":
    main()
