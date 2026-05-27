#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# Zyvora Terminal — per-deploy script (GHCR-pull, no build)
#
# Usage:
#   ./deploy.sh                  # pull latest, restart, prune old images
#   ./deploy.sh sha-718bd77      # roll back / pin to a specific image tag
#
# Built images live at ghcr.io/mahendirank/ai-market-terminal — see
# .github/workflows/deploy.yml for what gets published per push to main.
#
# First-time install on a fresh VPS: use bootstrap.sh, then this script for
# every subsequent update. See VPS_DEPLOYMENT.md for the full guide.
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/zyvora}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
SERVICE="${SERVICE:-market-terminal}"
HEALTH_PATH="${HEALTH_PATH:-/api/health}"
HEALTH_WAIT_SECS="${HEALTH_WAIT_SECS:-120}"
TAG="${1:-latest}"

cd "$INSTALL_DIR"

step()  { echo; echo "════ $1"; }
ok()    { echo "  ✓ $1"; }
fail()  { echo "  ✗ $1" >&2; exit 1; }

# ─── 1. Sanity checks ───────────────────────────────────────────
step "1/6 sanity checks"
[ -f "$COMPOSE_FILE" ] || fail "$COMPOSE_FILE not found in $INSTALL_DIR"
[ -f ".env" ]          || fail ".env not found — run bootstrap.sh first"
command -v docker >/dev/null || fail "docker not installed"
ok "running from $INSTALL_DIR with tag '$TAG'"

# ─── 2. Pull the requested image tag ────────────────────────────
step "2/6 pulling ghcr.io/mahendirank/ai-market-terminal:$TAG"
# IMAGE_TAG is read by docker-compose.prod.yml's ${IMAGE_TAG:-latest}.
# Exporting here lets the same compose file pin to any tag from CI.
export IMAGE_TAG="$TAG"
docker compose -f "$COMPOSE_FILE" pull "$SERVICE" 2>&1 | tail -8
ok "pulled"

# ─── 3. Bring the stack up (no rebuild) ─────────────────────────
step "3/6 rolling stack to new image"
docker compose -f "$COMPOSE_FILE" up -d --no-build --remove-orphans 2>&1 | tail -8
ok "containers started"

# ─── 4. Wait for healthy state ──────────────────────────────────
# The container declares `expose: [8001]` (not `ports: 8001:8001`), so the
# FastAPI app is only reachable on the docker-internal network — host-side
# `curl localhost:8001` would always time out and report a false failure
# (this exact bug masked successful deploys for hours before being caught).
# Use `docker exec` so the curl runs INSIDE the container where localhost
# IS the app. Falls through gracefully if the container is mid-startup:
# any non-zero exit (no-such-container, connection-refused, 5xx) leaves
# last_status empty and the loop retries.
step "4/6 waiting for /api/health (up to ${HEALTH_WAIT_SECS}s)"
deadline=$(( $(date +%s) + HEALTH_WAIT_SECS ))
last_status=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  if docker exec "$SERVICE" curl -fsS --max-time 5 "http://localhost:8001${HEALTH_PATH}" >/dev/null 2>&1; then
    last_status="healthy"
    break
  fi
  # Belt-and-braces: also accept Docker's own healthcheck reporting "healthy"
  # in case curl inside the container is briefly unavailable (e.g. during
  # exec-stream contention). Either signal is enough to call it good.
  state=$(docker inspect --format '{{.State.Health.Status}}' "$SERVICE" 2>/dev/null || echo "")
  if [ "$state" = "healthy" ]; then
    last_status="healthy"
    break
  fi
  sleep 5
done
if [ "$last_status" = "healthy" ]; then
  ok "container reports healthy"
else
  echo "  ✗ /api/health did not respond in ${HEALTH_WAIT_SECS}s — last 50 log lines:" >&2
  docker logs "$SERVICE" --tail 50 || true
  echo
  echo "  Container state:" >&2
  docker compose -f "$COMPOSE_FILE" ps
  echo
  echo "  Rollback: ./deploy.sh <previous-sha-tag>" >&2
  exit 1
fi

# ─── 5. Prune old images (free disk on small VPSes) ─────────────
step "5/6 pruning old images"
# --filter "until=24h" so a fresh rollback can still pull from cache for
# the first 24h without re-downloading from GHCR.
docker image prune -af --filter "until=24h" 2>&1 | tail -3 || true
ok "prune complete"

# ─── 6. Summary ─────────────────────────────────────────────────
step "6/6 deploy complete"
docker compose -f "$COMPOSE_FILE" ps
echo
echo "  Image: ghcr.io/mahendirank/ai-market-terminal:$TAG"
echo "  Health: $(docker exec "$SERVICE" curl -fsS --max-time 3 "http://localhost:8001${HEALTH_PATH}" 2>/dev/null | head -c 200 || echo 'unreachable')"
echo
echo "  Tail live logs:    docker logs -f $SERVICE"
echo "  Rollback:          ./deploy.sh sha-XXXXXXX"
