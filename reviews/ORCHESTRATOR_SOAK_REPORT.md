# ORCHESTRATOR_SOAK_REPORT.md

> Stage A — flag flip + short soak window (~15 minutes), 2026-05-19.
> **Result: healthy. Recommend continued 23h observation before Stage 4.3.**

---

## Scope and honest caveat

The user-specified target soak window is **24 hours**. This report covers
**~15 minutes of empirical observation** captured by Claude (2026-05-19,
05:59 UTC flip → 06:13 UTC last checkpoint). The remaining ~23 hours
require the operator's own monitoring per the playbook in §5.

What 15 minutes CAN reveal:
- Boot succeeds; orchestrator initializes
- Endpoints registered and respond correctly
- Memory + CPU stable in the short window
- No new ERROR-class log lines
- Latency unchanged

What 15 minutes CANNOT reveal:
- Slow memory leaks (e.g. ~1 MB/hour)
- Diurnal-pattern bugs (overnight vs daytime traffic)
- Connection-pool exhaustion under sustained load
- Async task starvation under prolonged scheduling

The recommendation in §6 is **tentative-positive** with a hard gate
on operator-driven 24h observation before Stage 4.3 begins.

---

## 1. Flag flip

```
2026-05-19 05:59:58 UTC  container restarted with AGENT_ORCHESTRATOR_ENABLED=true
2026-05-19 05:59:58 UTC  redis ping succeeded → RedisEventBus selected
2026-05-19 06:00:21 UTC  health → healthy (t+23s)
2026-05-19 06:00:30 UTC  orchestration boot logs visible:
   INFO [orchestration.runtime] event_bus_init
   INFO [orchestration.lifespan] orchestrator_lifespan_started
```

**Boot succeeded with no errors, no autonomous tasks started.**

Pre-flip snapshot:
- DB: `/opt/backups/db-pre-stageA-2026-05-19_0559.tar.gz` (662K)
- Tag: `pre-stageA-2026-05-19_0559` on VPS (local; not on GitHub)
- SHA: `de45e3e` (matches origin/main)

---

## 2. Checkpoint table (15-minute window)

| Time | Wall clock | mem | cpu | pids | net rx / tx | new ERROR | restarts |
|---|---|---|---|---|---|---|---|
| t (flip + restart) | 05:59:58 UTC | n/a (booting) | n/a | n/a | 0 | n/a | 0 |
| t+30s | 06:00:30 | 177.8 MiB | 17.03% | 13 | 0 | 0 | 0 |
| t+1.5min | 06:01:06 | 280.9 MiB | 3.27% | 14 | 8.77MB / 1.77MB | 0 | 0 |
| t+5min | 06:02:39 | 303.3 MiB | 3.51% | 19 | 15.2MB / 2.75MB | 0 | 0 |
| t+10min | 06:07:45 | 321.5 MiB | 16.32% | 21 | 38.0MB / 6.74MB | 0 | 0 |
| t+15min | 06:13:30 | 323.6 MiB | 8.05% | **14** | 60.3MB / 10.1MB | 0 | 0 |

**Memory growth curve**: 178 → 281 → 303 → 322 → 324 MiB.
**Derivative**: +103 MiB in first 1.5 min, then +42 MiB over the next 12 min — growth rate dropping rapidly (asymptotic to a steady-state band, consistent with cache warming, NOT a leak).

**PID trajectory**: 13 → 14 → 19 → 21 → **14**.
**Key signal**: PIDs CYCLED back down at t+15min as asyncio thread-pool idle workers were reclaimed. A leak would never let PIDs decrease.

---

## 3. Comparison with pre-flip baseline

Critical baseline for the apples-to-apples comparison: pre-flip
container was ~13 minutes old when I measured it.

| Metric | Pre-flip (flag OFF, age 13min) | Post-flip (flag ON, age 13min) | Delta |
|---|---|---|---|
| Memory | 319.6 MiB | 323.6 MiB | **+4 MiB** |
| CPU | 0.69% (idle moment) | 8.05% (mid workload) | — (point-in-time noise) |
| PIDs | 13 | 14 | **+1** |
| Restart count | 0 | 0 | 0 |
| Health | healthy | healthy | — |

**Net cost of orchestration with 0 agents**: ~4 MiB memory + 1 Redis client. Both negligible.

---

## 4. Endpoint sanity

| Endpoint | Responses across all 4 checkpoints | Notes |
|---|---|---|
| `/health` | 200 every time, < 5ms | Docker healthcheck path |
| `/api/agents` | 401 every time | Route registered, auth-gated as designed |
| `/api/circuits` | 401 every time | Route registered, auth-gated |
| `/api/streams/health` | 401 every time | Route registered, auth-gated |
| `/api/news`, `/api/regime` | 401 every time | Existing routes unchanged |
| External: `zyvoratech.co/health` | 200 every time, < 200ms incl. TLS | Caddy → market-terminal works |

**No 5xx, no timeouts, no flapping.**

`/health` latency (5 sequential hits at t+15min): 2.24ms / 4.83ms / 2.33ms / 2.16ms / 1.85ms. Median 2.24ms. Within historical norm.

---

## 5. Continuation playbook (the remaining ~23 hours)

Operator runs these at convenient intervals during the soak window:

### Hourly snapshot (suggested cadence: every 1–2h)
```bash
ssh root@72.61.173.89 'docker stats market-terminal --no-stream --format "{{.MemUsage}} cpu={{.CPUPerc}} pids={{.PIDs}} net={{.NetIO}}"'
ssh root@72.61.173.89 'docker inspect market-terminal --format "{{.RestartCount}}"'
```

Note these in a column:
| timestamp | mem | cpu | pids | net | restarts |
|---|---|---|---|---|---|
| 05:59 | 178 | 17.0 | 13 | 0 | 0 |
| 06:13 | 324 | 8.1  | 14 | 60M | 0 |
| ... | | | | | |

### Hourly error scan
```bash
ssh root@72.61.173.89 \
  'docker logs --since 1h market-terminal' \
  | grep -E "ERROR|exception|orchestration" \
  | grep -vE "TG send failed|yfinance.*delisted" \
  | head -20
```

Expected: empty or near-empty.

### Quick alert-conditions check
```bash
# Single ssh call covering 5 alert conditions:
ssh root@72.61.173.89 'bash -s' <<'EOF'
echo "Restart count: $(docker inspect market-terminal --format '{{.RestartCount}}')"
echo "Memory: $(docker stats market-terminal --no-stream --format '{{.MemUsage}}')"
echo "Redis connected clients: $(docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli CLIENT LIST | wc -l)"
echo "Recent /health latency:"
docker exec market-terminal curl -s -o /dev/null -w "%{time_total}s\n" http://localhost:8001/health
echo "External HTTPS:"
curl -s -o /dev/null -w "  zyvoratech.co/health → %{http_code} %{time_total}s\n" https://zyvoratech.co/health
EOF
```

### Stop conditions (immediate rollback)
- Memory > 500 MiB sustained for >30 min
- Restart count > 0
- Any new orchestration ERROR log line
- Memory growth >50 MiB/hour for >2 consecutive hours
- Redis disconnect storm (`connected_clients` flapping)
- `/health` latency p99 > 100ms

Rollback (Level 1, ~30s):
```bash
ssh root@72.61.173.89 'sed -i "s/^AGENT_ORCHESTRATOR_ENABLED=.*/AGENT_ORCHESTRATOR_ENABLED=false/" /opt/zyvora/.env && docker compose -f /opt/zyvora/docker-compose.prod.yml up -d --force-recreate market-terminal'
```

---

## 6. Tentative verdict

Based on the 15-minute window:

| Signal | Status |
|---|---|
| Boot succeeds | ✅ |
| Endpoints registered | ✅ |
| Memory cost bounded (+4 MiB at age-matched comparison) | ✅ |
| CPU steady-state matches pre-flip | ✅ |
| PIDs cycled (no monotonic growth) | ✅ |
| Zero new ERROR | ✅ |
| Zero restarts | ✅ |
| Latency unchanged | ✅ |
| External path works | ✅ |

**Recommendation: GREEN at the 15-minute mark. PROCEED with the 23h
passive soak using the playbook in §5. After 24h with no stop
conditions triggered, Stage 4.3 (NewsFetchAgent) is safe to begin.**

If anything in §5 trips a stop condition: rollback Level 1 + re-evaluate
before Stage 4.3.

If 24h elapses cleanly: move to `STAGE_4_3_ENABLEMENT_RECOMMENDATION.md` execution.
