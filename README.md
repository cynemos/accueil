# Page d'accueil de cyneprod.org

Page statique qui présente les applications hébergées sur le serveur personnel et y renvoie.

🌐 **En ligne : [cyneprod.org](https://cyneprod.org)**

- `index.html` : la page (aucune dépendance externe, thèmes clair et sombre).
- `accueil/` : les images des cartes, l'icône et la page d'état `etat.html`.
- `scripts/installer_nginx.sh` : configuration nginx et certificat (déjà appliquée, demande sudo).
- `scripts/deployer.sh` : mise à jour de la page sur le serveur.
- `scripts/etat_serveur.py` : relevé de l'état du serveur, lancé chaque minute par cron.

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

Pour ajouter une application au relevé : listes `APPLICATIONS`, `SAUVEGARDES` et `PROJETS` en
haut du script. Si le fichier n'existe pas ou n'est plus à jour depuis 5 minutes, la page le dit
(« Relevé interrompu ») ou masque le bloc.

## Mettre en ligne

Pousser sur `main`, puis sur le serveur :

```bash
~/apps/accueil/scripts/deployer.sh
```

## Aperçu local

```bash
python -m http.server 4326 --directory .
```
