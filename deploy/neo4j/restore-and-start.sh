#!/bin/bash
# Seed / restore before the server starts (neo4j-admin load needs the database offline).
#   1. /data/import/neo4j.dump present  -> manual restore (uploaded by an operator), then renamed
#   2. baked /seed/neo4j.dump whose hash is not yet in /data/.seeded -> first-boot seed
# Then hand over to the official entrypoint.
set -euo pipefail
load() {
  echo "[restore] loading $1/neo4j.dump into database neo4j (overwrite)"
  neo4j-admin database load neo4j --from-path="$1" --overwrite-destination=true
  chown -R neo4j:neo4j /data/databases /data/transactions 2>/dev/null || true
  echo "[restore] done"
}
if [ -f /data/import/neo4j.dump ]; then
  load /data/import
  mv /data/import/neo4j.dump "/data/import/neo4j.dump.loaded-$(date +%Y%m%dT%H%M%S)"
elif [ -f /seed/neo4j.dump ]; then
  SHA=$(cat /seed/neo4j.dump.sha)
  if [ ! -f /data/.seeded ] || [ "$(cat /data/.seeded)" != "$SHA" ]; then
    load /seed
    echo "$SHA" > /data/.seeded
  else
    echo "[restore] seed $SHA already loaded — normal boot"
  fi
fi
exec /startup/docker-entrypoint.sh "$@"
