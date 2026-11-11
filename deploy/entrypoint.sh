#!/bin/sh
# Démarrage du conteneur.
# Le dossier /data est monté depuis l'hôte : Docker le crée souvent au nom de root, et une commande lancée
# en root (docker compose exec …) peut y laisser des fichiers root. L'application, elle, tourne sans
# privilèges : on lui rend donc la propriété de /data, puis on abandonne les droits root.
set -e
APP_UID=1000
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  if [ -n "$(find "$DATA_DIR" ! -user "$APP_UID" -print -quit)" ]; then
    chown -R "$APP_UID:$APP_UID" "$DATA_DIR"
  fi
  exec setpriv --reuid="$APP_UID" --regid="$APP_UID" --clear-groups "$@"
fi
exec "$@"
