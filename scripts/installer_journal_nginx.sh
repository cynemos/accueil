#!/bin/bash
# Ajoute à nginx un second journal qui note le nom du site visité, pour la fréquentation de la
# page d'état (scripts/etat_serveur.py). Le journal habituel access.log n'est pas modifié.
#
# À lancer une seule fois sur le serveur : sudo bash ~/apps/accueil/scripts/installer_journal_nginx.sh
# Le nouveau fichier /var/log/nginx/acces_sites.log est tourné chaque jour par la règle logrotate
# existante de nginx (/var/log/nginx/*.log, 14 jours), et lisible par le groupe adm.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo."; exit 1; }

CONF=/etc/nginx/conf.d/journal-sites.conf
cat > "$CONF" <<'EOF'
# Journal avec le nom du site (page d'état de cyneprod.org, scripts/etat_serveur.py)
log_format sites '$host $remote_addr [$time_local] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent"';
access_log /var/log/nginx/acces_sites.log sites;
EOF

if ! nginx -t 2>/dev/null; then
  nginx -t || true
  rm -f "$CONF"
  echo "ÉCHEC : configuration refusée par nginx, rien n'a été changé."
  exit 1
fi
systemctl reload nginx
sleep 2
touch /var/log/nginx/acces_sites.log
chown www-data:adm /var/log/nginx/acces_sites.log
chmod 0640 /var/log/nginx/acces_sites.log
echo "JOURNAL_OK : /var/log/nginx/acces_sites.log en place"
