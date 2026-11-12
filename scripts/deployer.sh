#!/bin/bash
# Met à jour la page d'accueil sur le serveur après un push sur GitHub. Sans sudo.
# À lancer sur le serveur :   ~/apps/accueil/scripts/deployer.sh
# Le dossier ~/apps/accueil est un clone du dépôt ; nginx sert index.html et le dossier accueil/.
set -euo pipefail

SITE=/home/cynemos/apps/accueil
git -C "$SITE" fetch --quiet --tags origin main
git -C "$SITE" merge --ff-only origin/main | tail -1
echo "ACCUEIL_OK : $(git -C "$SITE" describe --tags --always) en place"
