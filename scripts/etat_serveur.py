#!/usr/bin/env python3
"""Relevé de l'état du serveur pour la page d'accueil. Sans sudo.

Lancé chaque minute par cron, il écrit accueil/etat.json (lu par la page) :
charge, mémoire, disque, température, réponse de chaque application, sauvegardes de la nuit,
certificat HTTPS, et un historique sur 24 heures (un point toutes les 5 minutes).

Le fichier est public : il ne contient ni version de logiciel, ni adresse, ni nom de machine,
seulement des mesures et des états.
"""
import json
import os
import re
import shutil
import ssl
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

SORTIE = Path(__file__).resolve().parent.parent / "accueil" / "etat.json"
HISTORIQUE_POINTS = 288          # 24 h à raison d'un point toutes les 5 minutes
HISTORIQUE_PAS = 300             # secondes

# nom affiché, adresse du site, adresse de contrôle de son API (ou None)
APPLICATIONS = [
    ("Page d'accueil", "https://cyneprod.org/", None),
    ("Lambretta", "https://lambretta.cyneprod.org/", "https://api.cyneprod.org/auth/v1/health"),
    ("Bultaco", "https://bultaco.cyneprod.org/", "https://api-bultaco.cyneprod.org/auth/v1/health"),
    ("AéroMag", "https://aeromag.cyneprod.org/", "https://api-aeromag.cyneprod.org/auth/v1/health"),
    ("Carnets d'entretien", "https://carnet.cyneprod.org/", "https://api-carnet.cyneprod.org/auth/v1/health"),
]
# nom affiché, fichier d'état écrit par le script de sauvegarde
SAUVEGARDES = [
    ("Lambretta", "~/sauvegardes/auto/ETAT"),
    ("Bultaco", "~/sauvegardes/auto-bultaco/ETAT"),
    ("AéroMag", "~/sauvegardes/auto-aeromag/ETAT"),
    ("Carnets d'entretien", "~/sauvegardes/auto-carnet/ETAT"),
]
# Conteneurs Supabase dont chaque application a besoin (suffixe = nom du projet)
SERVICES_REQUIS = ["db", "kong", "rest", "auth", "storage"]
PROJETS = {
    "supabase": SERVICES_REQUIS,
    "bultaco": SERVICES_REQUIS,
    "aeromag": SERVICES_REQUIS,
    "carnet": ["db", "kong", "rest", "auth"],  # pas de stockage de fichiers
}


def lire(chemin):
    try:
        return Path(chemin).read_text()
    except OSError:
        return ""


def processeur_pourcent(duree=1.0):
    """Occupation du processeur sur une seconde (tous cœurs confondus)."""
    def releve():
        valeurs = [int(v) for v in lire("/proc/stat").splitlines()[0].split()[1:]]
        return sum(valeurs), valeurs[3] + valeurs[4]          # total, inactif + attente disque
    total1, repos1 = releve()
    time.sleep(duree)
    total2, repos2 = releve()
    ecart = total2 - total1
    return round(100 * (1 - (repos2 - repos1) / ecart), 1) if ecart else 0.0


def memoire():
    info = {ligne.split(":")[0]: int(ligne.split()[1]) * 1024 for ligne in lire("/proc/meminfo").splitlines() if ":" in ligne}
    total, libre = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    echange_total, echange_libre = info.get("SwapTotal", 0), info.get("SwapFree", 0)
    return {
        "total": total,
        "utilisee": total - libre,
        "pourcent": round(100 * (total - libre) / total, 1) if total else 0,
        "echange_pourcent": round(100 * (echange_total - echange_libre) / echange_total, 1) if echange_total else 0,
    }


def disque(chemin):
    try:
        usage = shutil.disk_usage(chemin)
    except OSError:
        return None
    return {"total": usage.total, "utilise": usage.used, "libre": usage.free, "pourcent": round(100 * usage.used / usage.total, 1)}


def temperature():
    """Température du processeur en °C, si le matériel la donne."""
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        if lire(zone / "type").strip() == "x86_pkg_temp":
            try:
                return round(int(lire(zone / "temp")) / 1000)
            except ValueError:
                pass
    return None


def repond(url):
    """Interroge le serveur lui-même (nginx local), avec le vrai nom d'hôte. Renvoie (ok, millisecondes)."""
    hote = re.match(r"https://([^/]+)", url).group(1)
    debut = time.monotonic()
    try:
        code = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-m", "8", "-w", "%{http_code}", "--resolve", f"{hote}:443:127.0.0.1", url],
            capture_output=True, text=True, timeout=12,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        code = "000"
    return code == "200", round((time.monotonic() - debut) * 1000)


def applications():
    resultat = []
    for nom, site, api in APPLICATIONS:
        site_ok, site_ms = repond(site)
        entree = {"nom": nom, "site": site_ok, "ms": site_ms}
        if api:
            entree["api"], _ = repond(api)
        entree["ok"] = site_ok and entree.get("api", True)
        resultat.append(entree)
    return resultat


def conteneurs():
    try:
        sortie = subprocess.run(["docker", "ps", "--format", "{{.Names}}|{{.Status}}"], capture_output=True, text=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError):
        return {"actifs": 0, "en_defaut": 0, "manquants": sum(len(s) for s in PROJETS.values()), "ok": False}
    etats = dict(ligne.split("|", 1) for ligne in sortie.splitlines() if "|" in ligne)
    en_defaut = sum(1 for etat in etats.values() if "unhealthy" in etat or "Restarting" in etat)
    manquants = sum(1 for p, services in PROJETS.items() for s in services if f"supabase_{s}_{p}" not in etats)
    return {"actifs": len(etats), "en_defaut": en_defaut, "manquants": manquants, "ok": en_defaut == 0 and manquants == 0}


def sauvegardes():
    resultat = []
    for nom, fichier in SAUVEGARDES:
        texte = lire(os.path.expanduser(fichier)).strip()
        trouve = re.match(r"(OK|PARTIEL|ECHEC) (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", texte)
        if not trouve:
            resultat.append({"nom": nom, "etat": "inconnu", "date": None, "ok": False})
            continue
        # Les scripts de sauvegarde écrivent l'heure du serveur (UTC)
        date = datetime.strptime(trouve.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        age_heures = (datetime.now(timezone.utc) - date).total_seconds() / 3600
        etat = {"OK": "ok", "PARTIEL": "partielle", "ECHEC": "echec"}[trouve.group(1)]
        if etat == "ok" and age_heures > 30:
            etat = "ancienne"                                   # la sauvegarde de la nuit n'a pas eu lieu
        resultat.append({"nom": nom, "etat": etat, "date": date.isoformat(), "ok": etat == "ok"})
    return resultat


def certificat_jours():
    """Jours restants avant l'expiration du certificat HTTPS servi par nginx."""
    try:
        contexte = ssl.create_default_context()
        with socket.create_connection(("127.0.0.1", 443), timeout=8) as connexion:
            with contexte.wrap_socket(connexion, server_hostname="cyneprod.org") as tls:
                fin = ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
        return int((fin - time.time()) // 86400)
    except (OSError, ssl.SSLError, KeyError, ValueError):
        return None


def main():
    maintenant = datetime.now(timezone.utc)
    coeurs = os.cpu_count() or 1
    charge = [float(v) for v in lire("/proc/loadavg").split()[:3]] or [0.0, 0.0, 0.0]
    etat = {
        "genere": maintenant.isoformat(timespec="seconds"),
        "depuis": int(float(lire("/proc/uptime").split()[0])) if lire("/proc/uptime") else None,   # secondes depuis le démarrage
        "processeur": {"pourcent": processeur_pourcent(), "coeurs": coeurs, "charge": charge,
                       "charge_pourcent": round(100 * charge[0] / coeurs, 1), "temperature": temperature()},
        "memoire": memoire(),
        "disque": disque("/"),
        "nas": disque("/mnt/nas-sauvegardes") if os.path.ismount("/mnt/nas-sauvegardes") else None,
        "applications": applications(),
        "conteneurs": conteneurs(),
        "sauvegardes": sauvegardes(),
        "certificat_jours": certificat_jours(),
    }

    # Bilan : « panne » si un service ne répond pas, « attention » si un seuil est dépassé
    alertes, pannes = [], []
    pannes += [f"{a['nom']} ne répond pas" for a in etat["applications"] if not a["ok"]]
    if not etat["conteneurs"]["ok"]:
        pannes.append("un service de base de données est arrêté ou en défaut")
    alertes += [f"sauvegarde {s['nom']} : {s['etat']}" for s in etat["sauvegardes"] if not s["ok"]]
    if etat["disque"] and etat["disque"]["pourcent"] >= 85:
        alertes.append("disque presque plein")
    if etat["memoire"]["pourcent"] >= 90:
        alertes.append("mémoire presque pleine")
    if etat["nas"] is None:
        alertes.append("NAS de sauvegarde non monté")
    if etat["certificat_jours"] is not None and etat["certificat_jours"] < 15:
        alertes.append("certificat HTTPS bientôt expiré")
    if etat["processeur"]["temperature"] is not None and etat["processeur"]["temperature"] >= 80:
        alertes.append("processeur chaud")
    etat["bilan"] = "panne" if pannes else "attention" if alertes else "ok"
    etat["messages"] = pannes + alertes

    # Historique sur 24 h : repris du fichier précédent, un point toutes les 5 minutes
    historique = []
    try:
        historique = json.loads(SORTIE.read_text()).get("historique", [])
    except (OSError, ValueError):
        pass
    instant = int(maintenant.timestamp())
    if not historique or instant - historique[-1]["t"] >= HISTORIQUE_PAS - 30:
        historique.append({"t": instant, "cpu": etat["processeur"]["pourcent"], "mem": etat["memoire"]["pourcent"],
                           "ok": etat["bilan"] != "panne"})
    etat["historique"] = [p for p in historique if instant - p["t"] <= HISTORIQUE_POINTS * HISTORIQUE_PAS][-HISTORIQUE_POINTS:]

    # Écriture atomique : la page ne lit jamais un fichier à moitié écrit
    provisoire = SORTIE.with_suffix(".json.tmp")
    provisoire.write_text(json.dumps(etat, ensure_ascii=False, separators=(",", ":")))
    os.chmod(provisoire, 0o644)
    os.replace(provisoire, SORTIE)


if __name__ == "__main__":
    main()
