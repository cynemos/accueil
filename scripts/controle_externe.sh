#!/bin/sh
# Contrôle de cyneprod.org vu de l'extérieur du serveur, lancé par le NAS toutes les 5 minutes
# (Synology : Panneau de configuration > Planificateur de tâches > Script défini par l'utilisateur).
#
# Le serveur ne peut pas signaler sa propre panne (arrêt, plantage, relevé bloqué) : ce script,
# qui tourne sur une autre machine, le fait. Il vérifie que https://cyneprod.org répond par
# Internet et que le relevé d'état a moins de 10 minutes. En cas de panne (2 contrôles d'affilée),
# puis au retour à la normale, il envoie une alerte ntfy. Il écrit aussi son résultat dans
# DERNIER, à côté de lui, que le serveur lit (partage NFS) pour l'afficher sur la page d'état.
#
# Réglages dans controle.conf, à côté de ce script (jamais dans le dépôt) :
#   NTFY_CANAL=...          canal ntfy secret (le même que les alertes de sauvegarde)
#   SERVEUR_LOCAL=...       adresse du serveur sur le réseau de la maison (facultatif) : permet de
#                           distinguer « serveur arrêté » de « accès Internet en défaut »
# Limite : une coupure de courant ou d'Internet à la maison arrête aussi le NAS ou ses alertes.

ICI=$(cd "$(dirname "$0")" && pwd)
NTFY_CANAL=""
SERVEUR_LOCAL=""
[ -r "$ICI/controle.conf" ] && . "$ICI/controle.conf"
SITE=https://cyneprod.org
ETAT_ALERTE="$ICI/etat_alerte"         # nombre de contrôles en panne d'affilée, et alerte envoyée ou non
MAINTENANT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

alerte() {  # alerte <priorité> <étiquette> <titre> <message>
  [ -n "$NTFY_CANAL" ] || return 0
  curl -s -m 20 -o /dev/null -H "Title: $3" -H "Priority: $1" -H "Tags: $2" \
       -H "Click: $SITE/accueil/etat.html" -d "$4" "https://ntfy.sh/$NTFY_CANAL"
}

probleme=""
json=$(curl -s -m 20 -f "$SITE/accueil/etat.json")
if [ -z "$json" ]; then
  probleme="cyneprod.org ne répond pas par Internet"
  if [ -n "$SERVEUR_LOCAL" ] && curl -s -m 10 -f -o /dev/null --resolve "cyneprod.org:443:$SERVEUR_LOCAL" "$SITE/"; then
    probleme="$probleme (le serveur répond à la maison : box ou accès Internet en cause)"
  elif [ -n "$SERVEUR_LOCAL" ]; then
    probleme="$probleme, ni à la maison : serveur arrêté ou nginx en panne"
  fi
else
  # Fraîcheur du relevé (écrit chaque minute par etat_serveur.py)
  genere=$(printf '%s' "$json" | sed -n 's/.*"genere":"\([^"]*\)".*/\1/p')
  t=$(date -u -d "$genere" +%s 2>/dev/null)
  if [ -n "$t" ] && [ $(( $(date -u +%s) - t )) -gt 600 ]; then
    probleme="relevé d'état arrêté depuis $(( ($(date -u +%s) - t) / 60 )) min (serveur bloqué ou cron arrêté)"
  fi
fi

suivies=0; signalee=0
[ -r "$ETAT_ALERTE" ] && read -r suivies signalee < "$ETAT_ALERTE"
if [ -n "$probleme" ]; then
  suivies=$((suivies + 1))
  echo "PANNE $MAINTENANT : $probleme" > "$ICI/DERNIER"
  if [ "$suivies" -ge 2 ] && [ "$signalee" = 0 ]; then
    alerte 5 rotating_light "cyneprod.org injoignable" "$probleme (contrôle du NAS)" && signalee=1
  fi
else
  echo "OK $MAINTENANT" > "$ICI/DERNIER"
  if [ "$signalee" = 1 ]; then
    alerte 3 white_check_mark "cyneprod.org de nouveau joignable" "Le contrôle du NAS repasse au vert." && signalee=0
  fi
  [ "$signalee" = 0 ] && suivies=0
fi
echo "$suivies $signalee" > "$ETAT_ALERTE"
