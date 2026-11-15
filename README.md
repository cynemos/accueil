# Page d'accueil de cyneprod.org

Page statique qui présente les applications hébergées sur le serveur personnel et y renvoie.

🌐 **En ligne : [cyneprod.org](https://cyneprod.org)**

- `index.html` : la page (aucune dépendance externe, thèmes clair et sombre).
- `accueil/` : les images des cartes, l'icône et la page d'état `etat.html`.
- `scripts/installer_nginx.sh` : configuration nginx et certificat (déjà appliquée, demande sudo).
- `scripts/deployer.sh` : mise à jour de la page sur le serveur.
- `scripts/etat_serveur.py` : relevé de l'état du serveur, lancé chaque minute par cron.
- `scripts/test_restauration.py`, `scripts/controle_externe.sh` (NAS), `scripts/installer_journal_nginx.sh` : voir « Fiabilité et alertes ».

## Ajouter une application

Dans `index.html`, copier un bloc `<li>…</li>` de la liste `applis`, changer le lien, le titre,
le texte et l'image (1200 × 480 px, à déposer dans `accueil/`), puis choisir sa couleur avec une
classe sur le modèle de `.lambretta`, `.bultaco` et `.aeromag`.

## État du serveur

Le bas de la page d'accueil renvoie vers la page d'état
([cyneprod.org/accueil/etat.html](https://cyneprod.org/accueil/etat.html)) : processeur, charge,
mémoire et mémoire d'échange, disque, NAS de sauvegarde, température, durée de fonctionnement,
certificat HTTPS, réponse de chaque application, sauvegardes de la nuit, courbes processeur /
mémoire des dernières 24 heures, et le système : distribution, noyau, processeur, nouvelle version
annoncée, paquets à mettre à jour (dont sécurité) et redémarrage requis.

`scripts/etat_serveur.py` (sans sudo) écrit ces mesures dans `accueil/etat.json`, que la page lit
chaque minute. La liste des mises à jour vient de `apt list --upgradable` (cache d'apt, relu toutes
les heures). Ce fichier est public : il ne contient ni adresse ni nom de machine, mais il donne la
version du système et des paquets en attente. Il n'est pas versionné. Tâche cron :

```
* * * * * /usr/bin/python3 /home/cynemos/apps/accueil/scripts/etat_serveur.py >/dev/null 2>&1
```

Pour ajouter une application au relevé : listes `APPLICATIONS`, `SAUVEGARDES`, `PROJETS` et
`DONNEES` en haut de `etat_serveur.py`, et `APPLICATIONS` de `test_restauration.py`. Si le fichier
n'existe pas ou n'est plus à jour depuis 5 minutes, la page le dit (« Relevé interrompu »).

Une seule exécution à la fois : un verrou (`~/.local/state/accueil/`) écarte un second lancement.
Les données privées (historique horaire de disponibilité, état des alertes) restent dans
`~/.local/state/accueil/etat_prive.json`, jamais publiées.

### Fiabilité et alertes

- **Alertes sur le téléphone** (ntfy, canal secret de `~/.config/lambretta-alerte`) : incident
  après 2 relevés en panne d'affilée, retour à la normale, et chaque nouvel avertissement (disque,
  mises à jour de sécurité, redémarrage requis, certificat, test de restauration…). Les échecs de
  sauvegarde ont déjà leur propre alerte.
- **Disponibilité** sur 24 h, 7 jours et 30 jours : part des relevés sans panne, cumulée heure
  par heure.
- **Données par application** : taille de la base, des fichiers déposés et de la dernière
  sauvegarde (toutes les heures).
- **Test de restauration** (`scripts/test_restauration.py`, sans sudo, environ 5 minutes) : chaque
  semaine, la dernière sauvegarde de chaque application est restaurée dans un conteneur PostgreSQL
  jetable (même image que la base, sans réseau) ; empreintes, archive des fichiers et nombre de
  lignes de chaque table sont vérifiés. Résultat dans `~/sauvegardes/TEST_RESTAURATION.json`,
  alerte en cas d'échec. Essai sur une seule application : `test_restauration.py auto-bultaco`.
  Tâche cron (dimanche 5 h 30, heure du serveur, après les sauvegardes de la nuit) :

  ```
  30 5 * * 0 /usr/bin/python3 /home/cynemos/apps/accueil/scripts/test_restauration.py >/dev/null 2>&1
  ```

- **Contrôle externe** (`scripts/controle_externe.sh`) : lancé par le NAS toutes les 5 minutes,
  il vérifie que cyneprod.org répond par Internet et que le relevé est frais, alerte en cas de
  panne, et écrit son résultat dans `controle/DERNIER` sur le partage des sauvegardes, que le
  serveur affiche. Copie du script et réglages (`controle.conf`, non versionné) dans
  `/volume1/sauvegardes-serveur/controle/` sur le NAS (`/mnt/nas-sauvegardes/controle/` vu du
  serveur). Synology : Panneau de configuration > Planificateur de tâches > Créer > Tâche
  planifiée > Script défini par l'utilisateur, utilisateur `root`, toutes les 5 minutes,
  commande `sh /volume1/sauvegardes-serveur/controle/controle_externe.sh`.
- **Renouvellement du certificat** : le minuteur `certbot.timer` doit être actif.
- **Fréquentation** des dernières 24 heures par application (visiteurs distincts, pages vues,
  robots, erreurs serveur), tirée d'un journal nginx qui note le nom du site. Installation une
  seule fois : `sudo bash ~/apps/accueil/scripts/installer_journal_nginx.sh`. Ni les contrôles du
  serveur lui-même ni les visites depuis la maison ne sont comptés ; aucune adresse n'est publiée.

## Mettre en ligne

Pousser sur `main`, puis sur le serveur :

```bash
~/apps/accueil/scripts/deployer.sh
```

## Aperçu local

```bash
python -m http.server 4326 --directory .
```
