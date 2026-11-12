#!/bin/bash
# Met la page d'accueil sur cyneprod.org et déplace Lambretta sur lambretta.cyneprod.org.
# Demande sudo : à lancer par l'utilisateur, sur le serveur.
#
# - cyneprod.org/            → page d'accueil (/home/cynemos/apps/accueil)
# - cyneprod.org/autre-chose → renvoyé vers lambretta.cyneprod.org/autre-chose
#                              (les anciens liens et favoris de Lambretta continuent de marcher)
# - lambretta.cyneprod.org   → le site Lambretta, inchangé (même dossier, même API api.cyneprod.org)
#
# En cas d'erreur (configuration invalide ou certificat refusé), l'ancienne configuration est
# remise en place : Lambretta reste alors sur cyneprod.org comme avant.
set -euo pipefail

FICHIER=/etc/nginx/sites-available/cyneprod.org
CERTIFICAT=/etc/letsencrypt/live/cyneprod.org
SAUVEGARDE="$HOME/sauvegardes/nginx_cyneprod_$(date +%Y%m%d-%H%M%S)"
PUBLIC=$(getent ahostsv4 cyneprod.org | awk 'NR==1 {print $1}')

echo "=== Vérifications"
ip=$(getent ahostsv4 lambretta.cyneprod.org | awk 'NR==1 {print $1}')
[ "$ip" = "$PUBLIC" ] || { echo "lambretta.cyneprod.org ne pointe pas vers $PUBLIC (trouvé : ${ip:-rien})."; exit 1; }
[ -s /home/cynemos/apps/accueil/index.html ] || { echo "Page d'accueil absente de ~/apps/accueil."; exit 1; }
[ -s /home/cynemos/apps/lambretta/dist/index.html ] || { echo "Site Lambretta absent de ~/apps/lambretta/dist."; exit 1; }
sudo cp "$FICHIER" "$SAUVEGARDE"
echo "Ancienne configuration copiée dans $SAUVEGARDE"

retour_arriere() {
  echo "ERREUR : $1 — retour à l'ancienne configuration."
  sudo cp "$SAUVEGARDE" "$FICHIER"
  sudo nginx -t && sudo systemctl reload nginx
  echo "Ancienne configuration remise en place : Lambretta est de nouveau sur cyneprod.org."
  exit 1
}

echo "=== nginx"
sudo tee "$FICHIER" > /dev/null <<EOF
# Page d'accueil : présente les applications
server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name cyneprod.org www.cyneprod.org;

    ssl_certificate $CERTIFICAT/fullchain.pem;
    ssl_certificate_key $CERTIFICAT/privkey.pem;

    root /home/cynemos/apps/accueil;

    location = / {
        try_files /index.html =404;
    }
    # Images et icône de la page d'accueil
    location /accueil/ {
        expires 7d;
    }

    # Ancien accès au stockage de Lambretta par cyneprod.org/storage/ (conservé par prudence)
    location /storage/ {
        proxy_pass http://127.0.0.1:54321;

        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        client_max_body_size 100m;

        add_header Access-Control-Allow-Origin https://lambretta.cyneprod.org always;
        add_header Access-Control-Allow-Methods "GET, POST, OPTIONS" always;
        add_header Access-Control-Allow-Headers "*" always;

        if (\$request_method = OPTIONS) {
            return 204;
        }
    }

    # Anciennes adresses de Lambretta (cyneprod.org/pieces, /avant-apres…)
    location / {
        return 302 https://lambretta.cyneprod.org\$request_uri;
    }
}

# Site Lambretta
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name lambretta.cyneprod.org;

    ssl_certificate $CERTIFICAT/fullchain.pem;
    ssl_certificate_key $CERTIFICAT/privkey.pem;

    root /home/cynemos/apps/lambretta/dist;
    index index.html;

    # Une adresse par page (/pieces, /avant-apres…) : toutes renvoient vers l'application
    location / {
        try_files \$uri \$uri/ /index.html;
    }
    # Fichiers compilés : leur nom change à chaque version, le navigateur peut les garder
    location /assets/ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}

# HTTP → HTTPS
server {
    listen 80;
    listen [::]:80;
    server_name cyneprod.org www.cyneprod.org lambretta.cyneprod.org;
    return 301 https://\$host\$request_uri;
}
EOF
sudo nginx -t || retour_arriere "configuration nginx invalide"
sudo systemctl reload nginx

echo "=== Certificat (ajout de lambretta.cyneprod.org)"
sudo certbot certonly --nginx --cert-name cyneprod.org --expand \
  -d cyneprod.org -d www.cyneprod.org -d api.cyneprod.org \
  -d bultaco.cyneprod.org -d api-bultaco.cyneprod.org \
  -d lambretta.cyneprod.org || retour_arriere "certificat refusé"
sudo systemctl reload nginx

echo "=== Vérification"
R="--resolve cyneprod.org:443:127.0.0.1 --resolve lambretta.cyneprod.org:443:127.0.0.1 --resolve bultaco.cyneprod.org:443:127.0.0.1 --resolve api.cyneprod.org:443:127.0.0.1"
verifier() { resultat=$(curl -s $R -o /dev/null -m 15 -w "$3" "$1" || true); printf '%-46s %s\n' "$1" "$resultat"; [ "$resultat" = "$2" ]; }
ok=1
verifier https://cyneprod.org/ 200 '%{http_code}' || ok=0
verifier https://cyneprod.org/accueil/lambretta.jpg 200 '%{http_code}' || ok=0
verifier https://cyneprod.org/pieces https://lambretta.cyneprod.org/pieces '%{redirect_url}' || ok=0
verifier https://lambretta.cyneprod.org/ 200 '%{http_code}' || ok=0
verifier https://lambretta.cyneprod.org/pieces 200 '%{http_code}' || ok=0
verifier https://bultaco.cyneprod.org/ 200 '%{http_code}' || ok=0
verifier https://api.cyneprod.org/auth/v1/health 200 '%{http_code}' || ok=0
curl -s $R -m 15 https://cyneprod.org/ | grep -q 'Mes applications' && echo "cyneprod.org sert bien la page d'accueil" || ok=0
curl -s $R -m 15 https://lambretta.cyneprod.org/ | grep -q 'Lambretta LD125' && echo "lambretta.cyneprod.org sert bien Lambretta" || ok=0
[ "$ok" = 1 ] && echo "INSTALLATION_OK" || { echo "INSTALLATION À VÉRIFIER (pour revenir en arrière : sudo cp $SAUVEGARDE $FICHIER && sudo systemctl reload nginx)"; exit 1; }
