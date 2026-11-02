#!/usr/bin/env bash
# Nightly Postgres backup (architecture §7 "Backups"). Cron runs it at 03:00 (infra/vm/startup.sh).
# Dumps the app, keycloak and openfga databases with pg_dump -Fc to
# gs://$GCS_BACKUP_BUCKET/pg/<YYYY-MM-DD>/ and keeps the 7 most recent dates (today and 6 before).
#
# Restore one database from a chosen date (run from /opt/app):
#   gsutil cp gs://$GCS_BACKUP_BUCKET/pg/<YYYY-MM-DD>/<name>.dump /tmp/<name>.dump
#   docker compose -f infra/docker-compose.yml --env-file infra/.env exec -T postgres \
#     sh -c 'pg_restore --clean -U "$POSTGRES_USER" -d <db>' < /tmp/<name>.dump
#   make up
# <name> is app, keycloak or openfga; <db> is the same, except app restores into $POSTGRES_DB
# (write it as "$POSTGRES_DB" inside the single quotes). Neo4j is rebuilt by make seed.
set -euo pipefail
log() { echo "spinach-backup: $*"; }
trap 'log "FAILED at line $LINENO"' ERR

cd "$(dirname "$0")/../.."
ENV_FILE=infra/.env
COMPOSE=(docker compose -f infra/docker-compose.yml --env-file "$ENV_FILE")

# The bucket comes from the environment, else from the GCS_BACKUP_BUCKET line of infra/.env.
if [ -z "${GCS_BACKUP_BUCKET:-}" ] && [ -f "$ENV_FILE" ]; then
  GCS_BACKUP_BUCKET=$(sed -n 's/^GCS_BACKUP_BUCKET=//p' "$ENV_FILE" | tail -n 1)
fi
if [ -z "${GCS_BACKUP_BUCKET:-}" ]; then
  log "GCS_BACKUP_BUCKET is empty; skipping backup"
  exit 0
fi

today=$(date -u +%F)
dest="gs://$GCS_BACKUP_BUCKET/pg/$today/"
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

# The app database is $POSTGRES_DB inside the container; '' means "use it".
for db in '' keycloak openfga; do
  name=${db:-app}
  # shellcheck disable=SC2016 # expanded by the shell inside the container
  "${COMPOSE[@]}" exec -T postgres \
    sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "${1:-$POSTGRES_DB}"' sh "$db" \
    > "$workdir/$name.dump"
  log "dumped $name ($(wc -c < "$workdir/$name.dump") bytes)"
done

gsutil -q cp "$workdir"/*.dump "$dest"
log "copied dumps to $dest"

# Keep 7 dates: delete date folders before today - 6 days.
cutoff=$(date -u -d "$today - 6 days" +%F)
gsutil ls "gs://$GCS_BACKUP_BUCKET/pg/" | while read -r folder; do
  d=$(basename "$folder")
  if [[ "$d" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && [[ "$d" < "$cutoff" ]]; then
    gsutil -q -m rm -r "$folder"
    log "deleted $folder"
  fi
done

log "done"
