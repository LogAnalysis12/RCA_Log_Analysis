#!/usr/bin/env bash
set -euo pipefail
command -v docker >/dev/null || { echo "Docker CLI is required." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker daemon is not running or is inaccessible." >&2; exit 1; }
: "${NEO4J_PASSWORD:?Set NEO4J_PASSWORD for the disposable test database}"
image="${NEO4J_IMAGE:-neo4j:5}"
docker rm -f neo4j-test >/dev/null 2>&1 || true
cleanup() { docker rm -f neo4j-test >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run --name neo4j-test -p 17474:7474 -p 17687:7687 -e "NEO4J_AUTH=neo4j/${NEO4J_PASSWORD}" -d "$image" >/dev/null
for attempt in $(seq 1 90); do
  if docker exec neo4j-test cypher-shell -u neo4j -p "$NEO4J_PASSWORD" 'RETURN 1' >/dev/null 2>&1; then break; fi
  if [ "$attempt" -eq 90 ]; then echo "Neo4j did not become ready within 90 seconds." >&2; exit 1; fi
  sleep 1
done
export NEO4J_TEST_URI=bolt://localhost:17687
export NEO4J_TEST_ALLOW_WIPE=1
python -m pytest -m "neo4j or neo4j_real" -v
