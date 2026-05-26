# VPS Deployment Guide

The terminal runs on Hostinger VPS `72.61.173.89` at `/opt/zyvora`,
fronted by Caddy with auto-HTTPS for `zyvoratech.co`.

**As of 2026-05-26, builds happen in GitHub Actions, not on the VPS.**
The VPS pulls a pre-built image from GHCR. Per-deploy downtime dropped from
~5 min (build + warm) to ~30s (pull + warm).

```
┌────────────────┐      git push       ┌──────────────────┐
│ your laptop    │ ───────────────────▶│ github.com/main  │
└────────────────┘                     └────────┬─────────┘
                                                │
                                     Actions builds image
                                                │
                                                ▼
                                       ┌────────────────┐
                                       │  ghcr.io       │
                                       │  :latest       │
                                       │  :sha-XXXXXXX  │
                                       │  :YYYYMMDD     │
                                       └────────┬───────┘
                                                │
                            ./deploy.sh         │
                          ┌─────────────────────┴────┐
                          ▼                          ▼
                  ┌────────────┐             ┌────────────┐
                  │ VPS pulls  │             │ rollback:  │
                  │ :latest    │             │ ./deploy.sh│
                  │ → up -d    │             │ sha-XXX    │
                  └────────────┘             └────────────┘
```

---

## One-time setup on a fresh VPS

Use `bootstrap.sh` exactly once:

```bash
ssh root@72.61.173.89
curl -fsSL https://raw.githubusercontent.com/mahendirank/ai-market-terminal/main/bootstrap.sh | bash
```

This installs Docker, clones the repo to `/opt/zyvora`, prompts for `.env`
values, and starts the stack by pulling from GHCR.

---

## GHCR login (required only if the image is private)

The `ai-market-terminal` GHCR image is public by default for public-repo
Actions, so `docker pull` works without auth. If you make the repo private
or change the package visibility, the VPS needs a personal access token:

1. Create a [classic PAT](https://github.com/settings/tokens/new) with
   scope **`read:packages`** only (no other scopes — least-privilege).
2. On the VPS:
   ```bash
   echo "<YOUR_PAT>" | docker login ghcr.io -u mahendirank --password-stdin
   ```
3. The login is persisted in `/root/.docker/config.json` and `docker pull`
   will then succeed for private images.

You can verify with:
```bash
docker pull ghcr.io/mahendirank/ai-market-terminal:latest
```

---

## Normal deploy (every push to main)

GitHub Actions builds and publishes automatically on push to `main`. The
Actions tab on github.com shows when the new image is ready (~3–4 min).
Once it's green:

```bash
ssh root@72.61.173.89
cd /opt/zyvora
./deploy.sh
```

What `deploy.sh` does:
1. Sanity-checks `.env` and `docker-compose.prod.yml` are present.
2. `docker compose pull market-terminal` — fetches the new image.
3. `docker compose up -d --no-build --remove-orphans` — rolls the container.
4. Waits up to 120s for `/api/health` to respond.
5. `docker image prune -af --filter "until=24h"` — frees disk (keeps last
   24h of images cached locally for fast rollback).
6. Prints final container status + image tag in use.

Expected total time: 20–40 seconds, of which ~25s is the warm-up of
the in-process caches (news, regime, prices).

---

## Rollback (when a deploy goes sideways)

Every published image carries an immutable `sha-<7char>` tag. To roll
back, pin to the SHA you want:

```bash
cd /opt/zyvora
./deploy.sh sha-718bd77
```

Find the SHA you want:
- **Locally:**  `git log --oneline -10` (each line's first 7 chars are
  the tag suffix; the tag is `sha-<first7>`)
- **On the VPS:** `docker images ghcr.io/mahendirank/ai-market-terminal`
  shows every tag still cached on the host. Within 24h of a deploy, the
  previous image is always cached locally — rollback is instant, no GHCR
  fetch needed.
- **On GHCR:** github.com/users/mahendirank/packages/container/ai-market-terminal/versions

The pinned tag is captured in the `IMAGE_TAG` env var, so subsequent
plain `./deploy.sh` calls (no arg) will go back to `:latest` automatically
once the issue is fixed and a new image is published.

---

## Health verification

After any deploy, run these in order to confirm the stack is healthy:

```bash
# 1. All containers running and healthy?
docker compose -f docker-compose.prod.yml ps
# Expected: market-terminal = Up X seconds (healthy)
#           terminal-redis   = Up X seconds (healthy)
#           caddy            = Up X seconds

# 2. /api/health returns JSON with overall status?
curl -sf http://localhost:8001/api/health | python3 -m json.tool | head -30
# Expected: "status": "healthy" (or "degraded" briefly during warm-up)

# 3. External HTTPS works?
curl -sfI https://zyvoratech.co/ | head -5
# Expected: HTTP/2 200 or 307 (redirect to /login)
```

A `degraded` status that persists past 90s of boot usually means a
background loop is stuck. Check `bg_loops` in `/api/health` — any loop
with `"status": "no-heartbeat"` is the suspect. Tail its module's
logs:

```bash
docker logs market-terminal --tail 200 | grep -E "ERROR|continuous_refresh|alert_engine|morning_note"
```

---

## Troubleshooting

### `pull access denied` on `docker compose pull`
Image is private and the VPS has no GHCR login. See the GHCR login
section above.

### `./deploy.sh` says health timeout after 120s
The image started but the app isn't responding. Check why:
```bash
docker logs market-terminal --tail 100
```
- `FATAL: missing required environment variables` → fix `.env` and retry
- `ConnectionError: Redis` → check `terminal-redis` is up and healthy
- `ModuleNotFoundError` → image is corrupted, try rolling back

### Container restart-loops
`docker ps -a | grep market-terminal` shows status `Restarting (78)`:
config error (likely missing `GROQ_API_KEY`). Fix `.env` then:
```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal
```

### `docker pull` is very slow / times out
GHCR is rate-limited per IP. If the VPS is sharing IP with other heavy
pullers, retry after 60s. Alternative: deploy a specific SHA from your
laptop with `docker save | ssh root@... 'docker load'` to bypass GHCR
entirely.

### Out of memory during deploy
`free -h` shows < 500 MB free. The new pull-only flow rarely OOMs (no
build), but old container shutdown + new image extraction can briefly
spike to ~1 GB. Add 4 GB of swap as a one-time fix:
```bash
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && \
swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab && \
swapon --show
```

### `/api/health` returns `status: degraded` and won't go green
This is informational — see which sub-check is failing:
```bash
curl -sf http://localhost:8001/api/health | python3 -m json.tool
```
Common transient cases (resolve within 2–3 min):
- `news.cache: cold` → first news fetch hasn't completed; harmless
- `live_data: stooq_blocked` → Stooq rate-limited; falls back to yfinance
- `bg_loops.*: no-heartbeat` → background loop hasn't ticked yet

Persistent cases (action needed):
- `redis.ok: false` → `docker compose restart redis` and check logs
- `groq.ok: false` → invalid or rate-limited GROQ_API_KEY

---

## Useful commands cheat sheet

```bash
# Tail logs live
docker compose -f docker-compose.prod.yml logs -f --tail 100 market-terminal

# Restart just the app (no pull, no rebuild)
docker compose -f docker-compose.prod.yml restart market-terminal

# Hard restart all services
docker compose -f docker-compose.prod.yml restart

# Show what image tag the running container is using
docker inspect market-terminal --format '{{.Config.Image}}'

# Show disk used by images (helps decide when to prune more aggressively)
docker system df

# Open a shell inside the running container
docker exec -it market-terminal bash

# Check redis connectivity from inside the app's container
docker exec market-terminal python3 -c "import redis, os; r=redis.from_url(os.environ['REDIS_URL']); print(r.ping())"
```

---

## What's intentionally NOT in this flow

- **No SSH-from-CI deploys.** GitHub Actions stops at publishing the
  image. A human runs `./deploy.sh` on the VPS. This is deliberate
  while the product is in `good for personal use` territory — no
  surprise prod changes when a PR auto-merges.
- **No multi-arch builds.** Only `linux/amd64`. Adding `linux/arm64`
  doubles build time and we don't have an ARM target.
- **No staging environment.** Per the existing CI comment ("prod
  doubles as staging"). When paid users arrive, add a staging VPS that
  pulls the `sha-<HEAD>` tag from a `staging` branch.
