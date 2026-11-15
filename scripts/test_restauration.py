#!/usr/bin/env python3
"""Test de restauration des sauvegardes, une fois par semaine (cron). Sans sudo.

Pour chaque application, la dernière sauvegarde de la nuit est restaurée dans un conteneur
PostgreSQL jetable (même image que la base en service, sans réseau), puis vérifiée :
  - empreintes SHA256SUMS de tous les fichiers ;
  - archive des fichiers lisible en entier ;
  - pour chaque table de la sauvegarde, le nombre de lignes restaurées est égal au nombre de
    lignes contenues dans le fichier de sauvegarde.
Le résultat va dans ~/sauvegardes/TEST_RESTAURATION.json (lu par etat_serveur.py), et une
alerte ntfy part en cas d'échec. Le conteneur est supprimé à la fin, même en cas d'erreur.

Usage : test_restauration.py [nom_dossier ...]   (par défaut toutes les applications)
"""
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# nom affiché, dossier des sauvegardes automatiques, conteneur de la base en service
APPLICATIONS = [
    ("Lambretta", "~/sauvegardes/auto", "supabase_db_supabase"),
    ("Bultaco", "~/sauvegardes/auto-bultaco", "supabase_db_bultaco"),
    ("AéroMag", "~/sauvegardes/auto-aeromag", "supabase_db_aeromag"),
    ("Carnets d'entretien", "~/sauvegardes/auto-carnet", "supabase_db_carnet"),
]
RESULTAT = Path("~/sauvegardes/TEST_RESTAURATION.json").expanduser()
CONTENEUR = "test-restauration"
# Tables vérifiées : données de l'application, comptes et index du stockage de fichiers
SCHEMAS = ("public", "auth", "storage")


def docker(*arguments, entree=None, delai=600):
    return subprocess.run(["docker", *arguments], input=entree, capture_output=True, timeout=delai)


def psql(requete):
    r = docker("exec", CONTENEUR, "psql", "-U", "supabase_admin", "-h", "127.0.0.1", "-d", "postgres",
               "-tAc", requete, delai=120)
    return r.stdout.decode().strip()


def lignes_dans_la_sauvegarde(dump):
    """Nombre de lignes de chaque table, lu dans le fichier de sauvegarde lui-même."""
    with open(dump, "rb") as f:
        sortie = docker("exec", "-i", CONTENEUR, "pg_restore", "--data-only", "-f", "-", entree=f.read()).stdout
    comptes, table = {}, None
    for ligne in sortie.split(b"\n"):
        if table is None:
            trouve = re.match(rb'COPY ("?[\w]+"?)\.("?[\w]+"?) ', ligne)
            if trouve:
                schema, nom = (x.decode().strip('"') for x in trouve.groups())
                table = f"{schema}.{nom}"
                comptes[table] = 0
        elif ligne == b"\\.":
            table = None
        else:
            comptes[table] += 1
    return {t: n for t, n in comptes.items() if t.split(".")[0] in SCHEMAS}


def tester(nom, dossier, conteneur_base):
    debut = time.monotonic()
    racine = Path(dossier).expanduser()
    sauvegardes = sorted(p for p in racine.glob("20*") if p.is_dir() and (p / "base.dump").exists())
    if not sauvegardes:
        return {"nom": nom, "ok": False, "erreur": "aucune sauvegarde trouvée"}
    s = sauvegardes[-1]
    resultat = {"nom": nom, "sauvegarde": s.name, "ok": False}

    # 1. Empreintes
    if (s / "SHA256SUMS").exists():
        r = subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=s, capture_output=True, text=True)
        resultat["empreintes"] = r.returncode == 0
        if r.returncode:
            resultat["erreur"] = "empreinte différente : " + (r.stdout + r.stderr).strip().splitlines()[0][:150]
            return resultat

    # 2. Archive des fichiers : lue jusqu'au bout
    archive = s / "fichiers.tar.gz"
    if archive.exists():
        try:
            with tarfile.open(archive) as tar:
                resultat["fichiers"] = sum(1 for m in tar if m.isfile())
        except (tarfile.TarError, OSError, EOFError) as e:
            resultat["erreur"] = f"archive des fichiers illisible : {e}"
            return resultat

    # 3. Restauration de la base dans un conteneur jetable
    image = docker("inspect", conteneur_base, "--format", "{{.Config.Image}}").stdout.decode().strip()
    if not image:
        resultat["erreur"] = "image de la base introuvable"
        return resultat
    docker("rm", "-f", CONTENEUR)
    try:
        r = docker("run", "-d", "--name", CONTENEUR, "--network", "none", "--memory", "1g",
                   "-e", "POSTGRES_PASSWORD=test-restauration-jetable", image)
        if r.returncode:
            resultat["erreur"] = "conteneur de test non démarré"
            return resultat
        # L'image initialise ses schémas au premier démarrage : attendre qu'elle soit prête
        for _ in range(120):
            if psql("select 1") == "1" and psql("select count(*) from pg_roles where rolname='authenticator'") == "1":
                break
            time.sleep(2)
        else:
            resultat["erreur"] = "base de test jamais prête"
            return resultat
        time.sleep(5)
        with open(s / "roles.sql", "rb") as f:
            docker("exec", "-i", CONTENEUR, "psql", "-U", "supabase_admin", "-h", "127.0.0.1", "-d", "postgres",
                   "-q", entree=f.read())
        with open(s / "base.dump", "rb") as f:
            # Les schémas déjà créés par l'image donnent des erreurs « already exists », sans gravité :
            # le contrôle porte sur les données effectivement restaurées
            docker("exec", "-i", CONTENEUR, "pg_restore", "-U", "supabase_admin", "-h", "127.0.0.1",
                   "-d", "postgres", "--clean", "--if-exists", entree=f.read())
        attendu = lignes_dans_la_sauvegarde(s / "base.dump")
        ecarts = []
        for table, n in sorted(attendu.items()):
            schema, nom_table = table.split(".")
            obtenu = psql(f'select count(*) from "{schema}"."{nom_table}"')
            if obtenu != str(n):
                ecarts.append(f"{table} : {obtenu or 'absente'} au lieu de {n}")
        resultat.update({
            "tables": len(attendu),
            "lignes": sum(attendu.values()),
            "ecarts": ecarts[:10],
            "ok": not ecarts and len(attendu) > 0,
        })
        if ecarts:
            resultat["erreur"] = f"{len(ecarts)} table(s) mal restaurée(s)"
    finally:
        docker("rm", "-f", CONTENEUR)
        resultat["duree"] = round(time.monotonic() - debut)
    return resultat


def alerter(titre, message):
    canal = ""
    for ligne in Path("~/.config/lambretta-alerte").expanduser().read_text().splitlines() if Path(
            "~/.config/lambretta-alerte").expanduser().exists() else []:
        if ligne.startswith("NTFY_CANAL="):
            canal = ligne.split("=", 1)[1].strip().strip('"\'')
    if not canal:
        return
    corps = json.dumps({"topic": canal, "priority": 4, "tags": ["floppy_disk"], "title": titre,
                        "message": message}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request("https://ntfy.sh", data=corps,
                                                      headers={"Content-Type": "application/json"}), timeout=20)
    except OSError:
        pass


def main():
    choix = sys.argv[1:]
    applis = [a for a in APPLICATIONS if not choix or Path(a[1]).name in choix]
    resultats = [tester(*a) for a in applis]
    for r in resultats:
        print(("OK    " if r["ok"] else "ÉCHEC ") + json.dumps(r, ensure_ascii=False))
    if choix:
        return                                           # essai partiel : rien n'est enregistré
    bilan = {"date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "ok": all(r["ok"] for r in resultats), "applications": resultats}
    provisoire = RESULTAT.with_suffix(".tmp")
    provisoire.write_text(json.dumps(bilan, ensure_ascii=False, indent=1))
    os.replace(provisoire, RESULTAT)
    if not bilan["ok"]:
        alerter("Test de restauration : ÉCHEC",
                " ; ".join(f"{r['nom']} : {r.get('erreur', 'échec')}" for r in resultats if not r["ok"]))


if __name__ == "__main__":
    main()
