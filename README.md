# Page d'accueil de cyneprod.org

Page statique qui présente les applications hébergées sur le serveur personnel et y renvoie.

🌐 **En ligne : [cyneprod.org](https://cyneprod.org)**

- `index.html` : la page (aucune dépendance externe, thèmes clair et sombre).
- `accueil/` : les images des cartes et l'icône.
- `scripts/installer_nginx.sh` : configuration nginx et certificat (déjà appliquée, demande sudo).
- `scripts/deployer.sh` : mise à jour de la page sur le serveur.

## Ajouter une application

Dans `index.html`, copier un bloc `<li>…</li>` de la liste `applis`, changer le lien, le titre,
le texte et l'image (1200 × 480 px, à déposer dans `accueil/`), puis choisir sa couleur avec une
classe sur le modèle de `.lambretta` et `.bultaco`.

## Mettre en ligne

Pousser sur `main`, puis sur le serveur :

```bash
~/apps/accueil/scripts/deployer.sh
```

## Aperçu local

```bash
python -m http.server 4326 --directory .
```
