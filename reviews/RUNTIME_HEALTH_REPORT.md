# RUNTIME_HEALTH_REPORT.md

> Post-deploy runtime measurements, 2026-05-19. Snapshot taken ~30s
> after container start. **All indicators healthy.**

---

## Container state

```
NAME              IMAGE                    STATUS                    PORTS
caddy             caddy:2-alpine           Up 4 days                 80, 443
market-terminal   zyvora-market-terminal   Up 30 seconds (healthy)   8001/tcp (internal)
terminal-redis    redis:7-alpine           Up 5 days (healthy)       6379/tcp (internal)
```

- `market-terminal`: NEW container (just restarted, healthy)
- `caddy` + `redis`: UNTOUCHED (continued through deploy, no restart)

---

## Resource utilization

| Metric | Value | Headroom |
|---|---|---|
| Container memory | 288.8 MiB | of 15.62 GiB (1.8% used) |
| Container CPU | 10.65% | of 100% on a 1-vCPU baseline |
| Restart count | 0 | (no crash loop) |
| Started at | 2026-05-19 05:16:49 UTC | ~30s before snapshot |
| Python version | 3.11.15 | matches Dockerfile |

**No memory pressure. No CPU saturation. No restart loop.**

---

## Subsystem health (`/api/health` output, abbreviated)

| Subsystem | Status | Note |
|---|---|---|
| Redis | ✅ `ok:true, latency_ms:3` | 1.59 MiB used (negligible) |
| SQLite | ✅ `ok:true, db_count:23` | 23 DBs, sizes ranging 12K–404K |
| Groq API key | configured | (assuming `.env` value is valid) |
| Telegram bot token | configured | But `TELEGRAM_CHAT_ID` is a placeholder — pre-existing |
| Live data feeds | not explicitly checked | Was running pre-deploy with same data sources |
| 8 background loops | not yet enumerated in this report | (Sprint 5 may add `/api/loops/health`) |

---

## Logging behavior verified

- **Format**: `console` (default; visually similar to pre-Sprint-2 stdlib logs)
- **Logger names visible**: `http.request` (from middleware), legacy `[NEWS]`/`[SIGNAL]`/`[ALERTS]` (from existing `print()` calls — unchanged)
- **Request ID prefix**: every middleware-emitted log line carries 12-char hex request ID
- **No errors from `logging_config` or `logging_middleware`** modules in 60s post-deploy window

---

## Docker logging driver — already configured

```json
{
  "Type": "json-file",
  "Config": {
    "max-file": "5",
    "max-size": "20m"
  }
}
```

5 files × 20MB = **100MB max log retention per container**. This is more conservative than my Sprint 2 recommendation (50m × 5 = 250MB), but it's safer for the VPS disk. **No further rotation work needed**.

---

## Network state

| Endpoint | Reachability | HTTP code |
|---|---|---|
| `localhost:8001/health` from container | ✅ via `docker exec curl` | 200 |
| `localhost:8001` from VPS host | ❌ not exposed to host (by design) | n/a |
| `https://zyvoratech.co/health` from internet | ✅ via Caddy | 200 |
| Inter-container Docker network: market-terminal ↔ redis | ✅ | redis ping = 3ms |

Topology is correct: only Caddy publishes ports (80/443). The app stays behind the proxy.

---

## Failure indicators — all clear

| Indicator | Check | Status |
|---|---|---|
| Container restart loop | `RestartCount` | 0 ✅ |
| Out-of-memory kill | `docker inspect ... .State.OOMKilled` | (not flagged) ✅ |
| Redis reconnect storms | grep logs for "reconnect" | none in 30s ✅ |
| Async deadlock | request_complete log line rate matches request rate | 4 lines/30s ≈ matches manual curls ✅ |
| Retry storms | grep "retry" in logs | none ✅ |
| Circuit-open spam | grep "circuit" in logs | none (no breakers wired yet) ✅ |

---

## Recommended next-hour observation

Run from VPS or local with SSH access:

```bash
# Watch for new ERROR lines (excluding pre-existing TG/yfinance noise)
ssh root@72.61.173.89 'docker logs -f market-terminal' \
  | grep -E "ERROR|WARNING" \
  | grep -vE "TG send failed|yfinance.*delisted"

# Watch resource usage
ssh root@72.61.173.89 'docker stats market-terminal'

# Snapshot health every 5 minutes
while true; do
  date
  ssh -o BatchMode=yes root@72.61.173.89 \
    "docker exec market-terminal curl -fsS http://localhost:8001/api/health | jq '.checks.redis.ok, .checks.sqlite.ok'"
  sleep 300
done
```

**Stop conditions** (any of these → roll back):
- New ERROR lines unrelated to TG / yfinance
- Memory growth >2× baseline within an hour
- CPU sustained >50% for >5 minutes
- Health endpoint returning non-200
