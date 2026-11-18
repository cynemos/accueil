#!/usr/bin/env python3
"""Relevé de l'état du serveur pour la page d'accueil. Sans sudo.

Lancé chaque minute par cron, il écrit accueil/etat.json (lu par la page) :
charge, mémoire, disque, température, réponse de chaque application, sauvegardes de la nuit,
certificat HTTPS, système (distribution, noyau, mises à jour en attente, redémarrage requis)
et un historique sur 24 heures (un point toutes les 5 minutes). Également : disponibilité sur
24 h / 7 j / 30 j, taille des données de chaque application (toutes les heures), fréquentation
des dernières 24 h (journal nginx), dernier test de restauration, contrôle externe fait depuis le
NAS, et renouvellement automatique du certificat. Envoie une alerte ntfy quand le bilan change.
Aussi : dernier passage des mises à jour automatiques, services systemd en échec, synchronisation
de l'heure, version de Docker et redémarrages des conteneurs, débit réseau, tendance de
remplissage du disque et du NAS, et un historique sur 7 jours (un point par heure).

Le fichier est public : il ne contient ni adresse, ni nom de machine. Il donne en revanche la
version du système et la liste des paquets à mettre à jour (demandé pour la page d'état).
"""
import fcntl
import json
import os
import platform
import re
import shutil
import ssl
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SORTIE = Path(__file__).resolve().parent.parent / "accueil" / "etat.json"
# Données privées (jamais publiées) : historique horaire, état des alertes, caches
PRIVE = Path("~/.local/state/accueil/etat_prive.json").expanduser()
VERROU = Path("~/.local/state/accueil/etat.verrou").expanduser()
HISTORIQUE_POINTS = 288          # 24 h à raison d'un point toutes les 5 minutes
HISTORIQUE_PAS = 300             # secondes
MISES_A_JOUR_PAS = 3600          # la liste des paquets à mettre à jour est relue toutes les heures
TAILLES_PAS = 3600               # tailles des bases et des fichiers : toutes les heures
FREQUENTATION_PAS = 300          # fréquentation : toutes les 5 minutes
DISPONIBILITE_HEURES = 30 * 24   # disponibilité gardée sur 30 jours, heure par heure
PANNES_AVANT_ALERTE = 2          # relevés en panne d'affilée avant l'alerte (évite les fausses alertes)
HISTORIQUE_7J_POINTS = 7 * 24    # historique sur 7 jours, un point par heure
TENDANCE_JOURS = 30              # tendance de remplissage des disques calculée sur 30 jours
PLEIN_ALERTE_JOURS = 60          # alerte si un disque doit être plein dans moins de 60 jours
MAJ_AUTO_ALERTE_HEURES = 48      # alerte si les mises à jour automatiques n'ont pas tourné depuis 2 jours
REDEMARRAGE_ALERTE = 3600        # alerte si un conteneur a redémarré tout seul dans la dernière heure

# Journal nginx avec le nom du site (installé par scripts/installer_journal_nginx.sh)
JOURNAL_NGINX = Path("/var/log/nginx/acces_sites.log")
TEST_RESTAURATION = Path("~/sauvegardes/TEST_RESTAURATION.json").expanduser()
CONTROLE_EXTERNE = Path("/mnt/nas-sauvegardes/controle/DERNIER")     # écrit par le NAS (controle_externe.sh)
ALERTE_CONFIG = Path("~/.config/lambretta-alerte").expanduser()       # NTFY_CANAL=..., secret

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
    "carnet": SERVICES_REQUIS,  # stockage de fichiers depuis Carnets v1.4.0
}
# Données de chaque application : nom affiché, projet Supabase, dossier des sauvegardes
DONNEES = [
    ("Lambretta", "supabase", "~/sauvegardes/auto"),
    ("Bultaco", "bultaco", "~/sauvegardes/auto-bultaco"),
    ("AéroMag", "aeromag", "~/sauvegardes/auto-aeromag"),
    ("Carnets d'entretien", "carnet", "~/sauvegardes/auto-carnet"),
]


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
    # Seuls les conteneurs Supabase des applications comptent (pas le conteneur jetable du test de restauration)
    etats = dict(ligne.split("|", 1) for ligne in sortie.splitlines() if ligne.startswith("supabase_") and "|" in ligne)
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


def os_release():
    """Champs de /etc/os-release (nom et version de la distribution)."""
    champs = {}
    for ligne in lire("/etc/os-release").splitlines():
        if "=" in ligne:
            cle, valeur = ligne.split("=", 1)
            champs[cle] = valeur.strip().strip('"')
    return champs


def modele_processeur():
    for ligne in lire("/proc/cpuinfo").splitlines():
        if ligne.startswith("model name"):
            return re.sub(r"\s+", " ", ligne.split(":", 1)[1]).strip()
    return None


def mises_a_jour():
    """Paquets à mettre à jour d'après le cache d'apt (sans sudo, sans rafraîchir le cache)."""
    try:
        sortie = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=60,
                                env={**os.environ, "LANG": "C", "LC_ALL": "C"}).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    paquets = []
    for ligne in sortie.splitlines():
        # nom/suite1,suite2 nouvelle_version architecture [upgradable from: ancienne_version]
        trouve = re.match(r"([^/\s]+)/(\S+) (\S+) \S+ \[upgradable from: ([^\]]+)\]", ligne)
        if trouve:
            paquets.append({"nom": trouve.group(1), "version": trouve.group(3), "depuis": trouve.group(4),
                            "securite": "-security" in trouve.group(2)})
    paquets.sort(key=lambda p: (not p["securite"], p["nom"]))
    # Date du dernier « apt update » : modification la plus récente des listes de paquets
    listes = [f.stat().st_mtime for f in Path("/var/lib/apt/lists").glob("*_Packages")] if Path("/var/lib/apt/lists").is_dir() else []
    return {
        "verifie": int(time.time()),
        "cache": datetime.fromtimestamp(max(listes), timezone.utc).isoformat(timespec="seconds") if listes else None,
        "total": len(paquets),
        "securite": sum(p["securite"] for p in paquets),
        "paquets": paquets,
    }


def nouvelle_version():
    """Nouvelle version de la distribution annoncée par Ubuntu (fichier tenu à jour par update-notifier)."""
    texte = lire("/var/lib/ubuntu-release-upgrader/release-upgrade-available").strip()
    trouve = re.search(r"New release '([^']+)' available", texte)
    return trouve.group(1) if trouve else None


def systeme(precedent):
    distribution = os_release()
    # apt est lent : la liste n'est relue que toutes les heures, sinon on reprend la précédente
    maj = (precedent or {}).get("mises_a_jour")
    if not maj or time.time() - maj.get("verifie", 0) >= MISES_A_JOUR_PAS:
        maj = mises_a_jour() or maj
    redemarrage = lire("/var/run/reboot-required.pkgs").split()
    return {
        "distribution": distribution.get("PRETTY_NAME") or platform.system(),
        "version": distribution.get("VERSION_ID"),
        "nom_code": distribution.get("VERSION_CODENAME"),
        "noyau": platform.release(),
        "architecture": platform.machine(),
        "processeur": modele_processeur(),
        "python": platform.python_version(),
        "redemarrage_requis": os.path.exists("/var/run/reboot-required"),
        "redemarrage_paquets": sorted(set(redemarrage)),
        "nouvelle_version": nouvelle_version(),
        "mises_a_jour": maj,
        "mises_a_jour_auto": mises_a_jour_automatiques(),
        "docker": version_docker(),
        "services_en_echec": services_en_echec(),
        "heure_synchronisee": heure_synchronisee(),
    }


def unite_active(unite):
    """L'unité systemd est-elle active ? None si systemctl ne répond pas."""
    try:
        r = subprocess.run(["systemctl", "is-active", unite], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"
    except (subprocess.SubprocessError, OSError):
        return None


def renouvellement_automatique():
    """Le minuteur de certbot, qui renouvelle le certificat, est-il actif ?"""
    return unite_active("certbot.timer")


def mises_a_jour_automatiques():
    """Dernier passage d'unattended-upgrades (horodatage tenu par apt) et état de son minuteur."""
    try:
        tampon = Path("/var/lib/apt/periodic/unattended-upgrades-stamp").stat().st_mtime
        dernier = datetime.fromtimestamp(tampon, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        dernier = None
    return {"dernier": dernier, "minuteur": unite_active("apt-daily-upgrade.timer")}


def services_en_echec():
    """Unités systemd en échec (nginx, cron, montage du NAS…). None si systemctl ne répond pas."""
    try:
        r = subprocess.run(["systemctl", "list-units", "--state=failed", "--no-legend", "--plain", "--no-pager"],
                           capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    return sorted(ligne.split()[0] for ligne in r.stdout.splitlines() if ligne.strip())


def heure_synchronisee():
    """L'horloge est-elle synchronisée par NTP ? None si inconnu."""
    try:
        r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                           capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return None
    return {"yes": True, "no": False}.get(r.stdout.strip())


def version_docker():
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError):
        return None
    return r.stdout.strip() or None


def redemarrages_conteneurs(prive, instant):
    """Redémarrages automatiques des conteneurs Supabase (compteur de Docker), avec l'heure du dernier vu.

    Docker ne date pas les redémarrages : on retient l'instant où le compteur a augmenté.
    """
    try:
        ids = subprocess.run(["docker", "ps", "-q", "--filter", "name=supabase_"],
                             capture_output=True, text=True, timeout=15).stdout.split()
        sortie = subprocess.run(["docker", "inspect", "--format", "{{.Name}}|{{.RestartCount}}", *ids],
                                capture_output=True, text=True, timeout=15).stdout if ids else ""
    except (subprocess.SubprocessError, OSError):
        return None
    ancien = prive.get("redemarrages", {})
    suivi, resultat = {}, []
    for ligne in sortie.splitlines():
        nom, _, compte = ligne.lstrip("/").partition("|")
        if not compte.isdigit():
            continue
        n = int(compte)
        precedent = ancien.get(nom, {"n": n, "dernier": None})
        # Compteur en hausse : nouveau redémarrage. En baisse : conteneur recréé, on repart de là.
        dernier = instant if n > precedent["n"] else precedent["dernier"]
        suivi[nom] = {"n": n, "dernier": dernier}
        if n or dernier:
            resultat.append({"nom": nom, "total": n, "dernier": dernier})
    prive["redemarrages"] = suivi
    return sorted(resultat, key=lambda r: -(r["dernier"] or 0))


def compteurs_reseau():
    """Octets reçus et envoyés par les cartes réseau physiques (sans les interfaces virtuelles de Docker)."""
    recu = envoye = 0
    for ligne in lire("/proc/net/dev").splitlines()[2:]:
        nom, _, valeurs = ligne.partition(":")
        if not Path(f"/sys/class/net/{nom.strip()}/device").exists():
            continue
        v = valeurs.split()
        recu, envoye = recu + int(v[0]), envoye + int(v[8])
    return recu, envoye


def tendance_remplissage(prive, instant, etat):
    """Jours avant que le disque et le NAS soient pleins, d'après la tendance des 30 derniers jours.

    Un point par jour (le dernier relevé du jour), pente par moindres carrés. Rien si l'occupation
    ne croît pas ou si l'on a moins de 3 jours de mesures.
    """
    points = prive.setdefault("occupation", [])
    jour = instant - instant % 86400
    valeurs = [etat[cle]["utilise"] if etat[cle] else None for cle in ("disque", "nas")]
    if not points or points[-1][0] != jour:
        points.append([jour, *valeurs])
    else:
        points[-1][1:] = [v if v is not None else a for v, a in zip(valeurs, points[-1][1:])]
    del points[:-TENDANCE_JOURS]
    for i, cle in ((1, "disque"), (2, "nas")):
        mesure = etat[cle]
        if not mesure:
            continue
        serie = [(p[0], p[i]) for p in points if p[i] is not None]
        mesure["plein_dans_jours"] = None
        mesure["tendance_jours"] = round((serie[-1][0] - serie[0][0]) / 86400) if serie else 0
        if len(serie) < 3:
            continue
        n = len(serie)
        mt, mv = sum(t for t, _ in serie) / n, sum(v for _, v in serie) / n
        ecart = sum((t - mt) ** 2 for t, _ in serie)
        pente = sum((t - mt) * (v - mv) for t, v in serie) / ecart * 86400 if ecart else 0   # octets par jour
        mesure["croissance_jour"] = round(pente)
        if pente > 0:
            mesure["plein_dans_jours"] = int(mesure["libre"] / pente)


def moyenne(points, cle):
    valeurs = [p[cle] for p in points if p.get(cle) is not None]
    return round(sum(valeurs) / len(valeurs), 1) if valeurs else None


def historique_7j(precedent, historique, instant):
    """Un point par heure terminée, calculé à partir des points de 5 minutes (gardés 24 h)."""
    points = precedent.get("historique_7j", [])
    heure_courante = instant - instant % 3600
    debut = max(points[-1]["t"] + 3600 if points else 0, heure_courante - 24 * 3600)
    for h in range(debut, heure_courante, 3600):
        dans = [p for p in historique if h <= p["t"] < h + 3600]
        if not dans:
            continue
        points.append({"t": h, "cpu": moyenne(dans, "cpu"), "cpu_max": max(p["cpu"] for p in dans),
                       "mem": moyenne(dans, "mem"), "disque": moyenne(dans, "disque"), "temp": moyenne(dans, "temp"),
                       "rx": moyenne(dans, "rx"), "tx": moyenne(dans, "tx"), "ok": all(p["ok"] for p in dans)})
    return [p for p in points if instant - p["t"] <= HISTORIQUE_7J_POINTS * 3600][-HISTORIQUE_7J_POINTS:]


def taille_dossier(chemin):
    try:
        return sum(f.stat().st_size for f in Path(chemin).rglob("*") if f.is_file())
    except OSError:
        return None


def tailles():
    """Pour chaque application : taille de la base, des fichiers déposés et de la dernière sauvegarde."""
    resultat = []
    for nom, projet, dossier in DONNEES:
        entree = {"nom": nom, "base": None, "fichiers": None, "sauvegarde": None, "sauvegarde_precedente": None}
        try:
            r = subprocess.run(["docker", "exec", f"supabase_db_{projet}", "psql", "-U", "postgres", "-tAc",
                                "select pg_database_size(current_database())"],
                               capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            entree["base"] = int(r.stdout.strip()) if r.stdout.strip().isdigit() else None
            r = subprocess.run(["docker", "exec", f"supabase_storage_{projet}", "du", "-sb", "/mnt"],
                               capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            entree["fichiers"] = int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout else None
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
        sauvegardes = sorted(p for p in Path(dossier).expanduser().glob("20*") if p.is_dir())
        if sauvegardes:
            entree["sauvegarde"] = taille_dossier(sauvegardes[-1])
        if len(sauvegardes) > 1:
            # Pour repérer une sauvegarde anormalement petite (données perdues, export incomplet)
            entree["sauvegarde_precedente"] = taille_dossier(sauvegardes[-2])
        resultat.append(entree)
    return resultat


MOIS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
LIGNE_JOURNAL = re.compile(r'(\S+) (\S+) \[(\d\d)/(\w{3})/(\d{4}):(\d\d):(\d\d):(\d\d) ([+-]\d{4})\] '
                           r'"(\S+) (\S+)[^"]*" (\d{3}) \S+ "[^"]*" "([^"]*)"')
ROBOT = re.compile(r"bot|crawl|spider|slurp|curl|wget|python|go-http|scan|monitor|headless|preview", re.I)


def frequentation(maintenant):
    """Visites des dernières 24 heures, par application, d'après le journal nginx.

    Ne compte ni les contrôles du serveur lui-même, ni les visites depuis la maison (adresse
    publique du serveur), et met les robots à part. Aucune adresse n'est publiée.
    """
    fichiers = [f for f in (JOURNAL_NGINX.with_name(JOURNAL_NGINX.name + ".1"), JOURNAL_NGINX) if f.exists()]
    if not fichiers:
        return None
    hotes = {}
    for nom, site, api in APPLICATIONS:
        hotes[re.match(r"https://([^/]+)", site).group(1)] = nom
        if api:
            hotes[re.match(r"https://([^/]+)", api).group(1)] = nom
    hotes.setdefault("www.cyneprod.org", hotes.get("cyneprod.org"))
    try:
        maison = socket.gethostbyname("cyneprod.org")
    except OSError:
        maison = None
    limite = maintenant.timestamp() - 86400
    stats = {nom: {"nom": nom, "pages": 0, "visiteurs": set(), "requetes": 0, "robots": 0, "erreurs": 0}
             for nom, _, _ in APPLICATIONS}
    for fichier in fichiers:
        try:
            texte = fichier.read_text(errors="replace")
        except OSError:
            continue
        for ligne in texte.splitlines():
            m = LIGNE_JOURNAL.match(ligne)
            if not m:
                continue
            hote, adresse, j, mois, a, h, mi, s, decalage, methode, chemin, code, agent = m.groups()
            app = hotes.get(hote.lower())
            if not app or adresse in ("127.0.0.1", "::1"):
                continue
            ts = datetime(int(a), MOIS.get(mois, 1), int(j), int(h), int(mi), int(s), tzinfo=timezone.utc).timestamp()
            ts -= (int(decalage[:3]) * 60 + int(decalage[0] + decalage[3:])) * 60
            if ts < limite:
                continue
            st = stats[app]
            st["requetes"] += 1
            if code.startswith("5"):
                st["erreurs"] += 1
            if ROBOT.search(agent) or not agent or agent == "-":
                st["robots"] += 1
                continue
            if adresse == maison or chemin.startswith("/accueil/etat.json"):
                continue
            # Une page vue : GET réussi d'une adresse sans extension (ou .html) d'un site, pas d'une API
            page = chemin.split("?")[0]
            est_api = page.startswith(("/rest/", "/auth/", "/storage/", "/realtime/"))
            if (methode == "GET" and code in ("200", "304") and not est_api
                    and ("." not in page.rsplit("/", 1)[-1] or page.endswith(".html"))):
                st["pages"] += 1
                st["visiteurs"].add((adresse, agent))
    resultat = []
    for st in stats.values():
        st["visiteurs"] = len(st["visiteurs"])
        resultat.append(st)
    return {"calcule": int(maintenant.timestamp()), "applications": resultat}


def disponibilite(prive, instant, en_service):
    """Part des relevés sans panne, sur 24 heures, 7 jours et 30 jours (cumul par heure)."""
    heures = prive.setdefault("disponibilite", [])
    heure = instant - instant % 3600
    if not heures or heures[-1][0] != heure:
        heures.append([heure, 0, 0])
    heures[-1][1] += 1
    heures[-1][2] += 1 if en_service else 0
    del heures[:-DISPONIBILITE_HEURES]

    def taux(jours):
        retenues = [h for h in heures if instant - h[0] < jours * 86400]
        total = sum(h[1] for h in retenues)
        return round(100 * sum(h[2] for h in retenues) / total, 2) if total else None
    premiere = heures[0][0] if heures else instant
    return {"24h": taux(1), "7j": taux(7), "30j": taux(30), "depuis": datetime.fromtimestamp(premiere, timezone.utc).isoformat(timespec="seconds")}


def test_restauration():
    """Résultat du dernier test de restauration (scripts/test_restauration.py, chaque semaine)."""
    try:
        d = json.loads(TEST_RESTAURATION.read_text())
    except (OSError, ValueError):
        return None
    age = time.time() - datetime.fromisoformat(d["date"]).timestamp()
    return {"date": d["date"], "ok": d["ok"], "ancien": age > 8 * 86400,
            "applications": [{k: a.get(k) for k in ("nom", "ok", "tables", "lignes", "fichiers", "erreur")}
                             for a in d.get("applications", [])]}


def controle_externe():
    """Dernier passage du contrôle fait depuis le NAS (« OK|PANNE date : détail »)."""
    try:
        texte = CONTROLE_EXTERNE.read_text().strip()
    except OSError:
        return None
    m = re.match(r"(OK|PANNE) (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)(?: : (.*))?", texte)
    if not m:
        return None
    date = datetime.strptime(m.group(2), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return {"ok": m.group(1) == "OK", "date": date.isoformat(), "detail": (m.group(3) or "")[:120],
            "ancien": time.time() - date.timestamp() > 15 * 60}


def envoyer_alerte(titre, message, priorite, etiquette):
    canal = ""
    if ALERTE_CONFIG.exists():
        for ligne in ALERTE_CONFIG.read_text().splitlines():
            if ligne.startswith("NTFY_CANAL="):
                canal = ligne.split("=", 1)[1].strip().strip("\"'")
    if not canal:
        return False
    corps = json.dumps({"topic": canal, "priority": priorite, "tags": [etiquette], "title": titre,
                        "message": message, "click": "https://cyneprod.org/accueil/etat.html"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request("https://ntfy.sh", data=corps,
                                                      headers={"Content-Type": "application/json"}), timeout=15)
        return True
    except OSError:
        return False


def alertes_telephone(prive, etat, alertes_cles):
    """Alerte ntfy aux changements : panne (après 2 relevés), retour à la normale, nouvel avertissement."""
    a = prive.setdefault("alertes", {"pannes_suivies": 0, "panne_signalee": False, "avertissements": []})
    if etat["bilan"] == "panne":
        a["pannes_suivies"] += 1
        if a["pannes_suivies"] >= PANNES_AVANT_ALERTE and not a["panne_signalee"]:
            if envoyer_alerte("cyneprod.org : incident", " ; ".join(etat["messages"]), 5, "rotating_light"):
                a["panne_signalee"] = True
    else:
        if a["panne_signalee"] and envoyer_alerte("cyneprod.org : rétabli", "Tous les services répondent de nouveau.",
                                                  3, "white_check_mark"):
            a["panne_signalee"] = False
        if not a["panne_signalee"]:
            a["pannes_suivies"] = 0
    # Avertissements : seulement ceux qui viennent d'apparaître (les échecs de sauvegarde ont déjà leur alerte)
    nouveaux = [texte for cle, texte in alertes_cles if cle not in a["avertissements"]]
    if nouveaux and envoyer_alerte("cyneprod.org : à surveiller", " ; ".join(nouveaux), 3, "warning"):
        a["avertissements"] = [cle for cle, _ in alertes_cles]
    elif not nouveaux:
        a["avertissements"] = [cle for cle, _ in alertes_cles]


def main():
    # Une seule exécution à la fois (deux lignes cron ou un relevé lent ne se marchent pas dessus)
    VERROU.parent.mkdir(parents=True, exist_ok=True)
    verrou = open(VERROU, "w")
    try:
        fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    maintenant = datetime.now(timezone.utc)
    try:
        precedent = json.loads(SORTIE.read_text())
    except (OSError, ValueError):
        precedent = {}
    try:
        prive = json.loads(PRIVE.read_text())
    except (OSError, ValueError):
        prive = {}
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
        "certificat_renouvellement": renouvellement_automatique(),
        "systeme": systeme(precedent.get("systeme")),
        "restauration": test_restauration(),
        "controle_externe": controle_externe(),
    }
    # Mesures plus lentes : recalculées à leur rythme, sinon reprises du relevé précédent
    instant = int(maintenant.timestamp())
    t = precedent.get("tailles")
    etat["tailles"] = tailles() if not t or instant - t.get("calcule", 0) >= TAILLES_PAS else t
    if etat["tailles"] is not t:
        etat["tailles"] = {"calcule": instant, "applications": etat["tailles"]}
    f = precedent.get("frequentation")
    etat["frequentation"] = (frequentation(maintenant) if not f or instant - f.get("calcule", 0) >= FREQUENTATION_PAS - 30
                             else f)
    etat["conteneurs"]["redemarrages"] = redemarrages_conteneurs(prive, instant)
    tendance_remplissage(prive, instant, etat)

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
    maj = etat["systeme"]["mises_a_jour"]
    if maj and maj["securite"]:
        alertes.append(f"{maj['securite']} mise(s) à jour de sécurité en attente")
    if etat["systeme"]["redemarrage_requis"]:
        alertes.append("redémarrage requis pour terminer des mises à jour")
    if etat["certificat_renouvellement"] is False:
        alertes.append("renouvellement automatique du certificat arrêté")
    r = etat["restauration"]
    if r and not r["ok"]:
        alertes.append("le dernier test de restauration a échoué")
    elif r and r["ancien"]:
        alertes.append("aucun test de restauration depuis plus d'une semaine")
    c = etat["controle_externe"]
    if c and c["ancien"]:
        alertes.append("le contrôle externe (NAS) ne passe plus")
    elif c and not c["ok"]:
        alertes.append("contrôle externe : " + (c["detail"] or "accès depuis Internet en défaut"))
    s = etat["systeme"]
    auto = s["mises_a_jour_auto"]
    if auto["minuteur"] is False:
        alertes.append("mises à jour automatiques désactivées")
    elif auto["dernier"] is None:
        alertes.append("mises à jour automatiques jamais lancées")
    elif instant - datetime.fromisoformat(auto["dernier"]).timestamp() > MAJ_AUTO_ALERTE_HEURES * 3600:
        alertes.append("mises à jour automatiques arrêtées depuis plus de 2 jours")
    if s["services_en_echec"]:
        alertes.append("service(s) système en échec : " + ", ".join(s["services_en_echec"]))
    if s["heure_synchronisee"] is False:
        alertes.append("horloge du serveur non synchronisée")
    for r in etat["conteneurs"]["redemarrages"] or []:
        if r["dernier"] and instant - r["dernier"] < REDEMARRAGE_ALERTE:
            alertes.append(f"le conteneur {r['nom']} a redémarré tout seul")
    for cle, nom in (("disque", "disque"), ("nas", "NAS de sauvegarde")):
        jours = (etat[cle] or {}).get("plein_dans_jours")
        if jours is not None and jours < PLEIN_ALERTE_JOURS:
            alertes.append(f"{nom} plein dans environ {jours} jours au rythme actuel")
    for a in etat["tailles"]["applications"]:
        actuelle, avant = a.get("sauvegarde"), a.get("sauvegarde_precedente")
        if actuelle is not None and avant and avant > 1024 ** 2 and actuelle < avant / 2:
            alertes.append(f"sauvegarde {a['nom']} deux fois plus petite que la précédente")
    etat["bilan"] = "panne" if pannes else "attention" if alertes else "ok"
    etat["messages"] = pannes + alertes

    # Alertes sur le téléphone. Clé sans chiffres : « 3 mises à jour » puis « 4 mises à jour » = même alerte.
    # Les échecs de sauvegarde sont déjà signalés par les scripts de sauvegarde eux-mêmes.
    cles = [(re.sub(r"\d+", "#", m), m) for m in alertes
            if not re.match(r"sauvegarde .* : (echec|partielle)$", m)]
    alertes_telephone(prive, etat, cles)
    etat["disponibilite"] = disponibilite(prive, instant, not pannes)

    # Historique sur 24 h : repris du fichier précédent, un point toutes les 5 minutes
    historique = precedent.get("historique", [])
    if not historique or instant - historique[-1]["t"] >= HISTORIQUE_PAS - 30:
        point = {"t": instant, "cpu": etat["processeur"]["pourcent"], "mem": etat["memoire"]["pourcent"],
                 "disque": etat["disque"]["pourcent"] if etat["disque"] else None,
                 "temp": etat["processeur"]["temperature"], "ok": etat["bilan"] != "panne"}
        # Débit réseau moyen depuis le point précédent, en octets par seconde (rien après un redémarrage)
        recu, envoye = compteurs_reseau()
        r = prive.get("reseau")
        if r and instant > r[0] and recu >= r[1] and envoye >= r[2]:
            point["rx"] = round((recu - r[1]) / (instant - r[0]))
            point["tx"] = round((envoye - r[2]) / (instant - r[0]))
        prive["reseau"] = [instant, recu, envoye]
        historique.append(point)
    etat["historique"] = [p for p in historique if instant - p["t"] <= HISTORIQUE_POINTS * HISTORIQUE_PAS][-HISTORIQUE_POINTS:]
    etat["historique_7j"] = historique_7j(precedent, etat["historique"], instant)

    # Écriture atomique : la page ne lit jamais un fichier à moitié écrit
    provisoire = SORTIE.with_suffix(".json.tmp")
    provisoire.write_text(json.dumps(etat, ensure_ascii=False, separators=(",", ":")))
    os.chmod(provisoire, 0o644)
    os.replace(provisoire, SORTIE)
    provisoire = PRIVE.with_suffix(".tmp")
    provisoire.write_text(json.dumps(prive, separators=(",", ":")))
    os.replace(provisoire, PRIVE)


if __name__ == "__main__":
    main()
