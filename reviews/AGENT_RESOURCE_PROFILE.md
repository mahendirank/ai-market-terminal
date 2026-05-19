# AGENT_RESOURCE_PROFILE.md

> Resource budget + tracking for NewsFetchAgent during the 48h dual-run.

---

## 1. Pre-flag-flip baseline (Stage A — orchestrator on, agent off)

Container 2026-05-19 ~30s post-restart with code-only deploy:

| Metric | Value |
|---|---|
| Memory | 182.1 MiB |
| CPU | 12.79% (initial workload) |
| PIDs | 13 |
| Restart count | 0 |
| Container start | 2026-05-19T16:19:05 UTC |

Baseline for the 48h dual-run begins **after** the operator flips
`AGENT_NEWS_FETCH_ENABLED=true`. Until then, no agent runs.

---

## 2. Expected resource cost when flag flips ON

### Per-tick (every 120s)

| Resource | Cost |
|---|---|
| Memory (peak during fetch) | +5-10 MiB transient (news list in memory) |
| CPU (during fetch) | 5-10% briefly (parsing 50-200 RSS items) |
| Network rx | 50-300 KB per fetch (RSS payloads) |
| Redis ops | 1 XADD (emit `news.raw` event) |
| Asyncio tasks | 1 transient (`asyncio.to_thread` worker) |
| Threads | 0-1 transient (thread pool reuses workers) |
| Log lines | 1 `news_agent_tick_complete` + maybe 1 from request middleware |

### Per-hour (30 ticks)

| Resource | Cost |
|---|---|
| Memory delta | < 5 MiB (after caches warm; should plateau) |
| CPU avg | < 0.5% additional |
| Network rx | ~3-9 MB |
| Redis stream depth | +30 events (capped at MAXLEN 5000) |
| Redis memory | +~30 KB (each event ~1KB) |
| Log volume | ~30 new structured lines |

### Per-24h

| Resource | Cost |
|---|---|
| Memory plateau | < 50 MiB cumulative growth |
| Network rx total | ~75-225 MB |
| Redis stream depth | plateaus at MAXLEN 5000 (~6 days of events) |
| Redis memory | ~5 MB at saturation |
| Log volume | ~720 new structured lines (≈ 200 KB) |

---

## 3. Budgets (from `MULTI_AGENT_PLAN.md §8`)

| Family | Memory budget | CPU budget | Network budget |
|---|---|---|---|
| News (full family — eventually 5 agents) | 100 MiB | 5% | 200 MB/24h |
| **Sprint 4.3 (one agent)** | < 30 MiB | < 1% | < 100 MB/24h |
| Container limit | 15.62 GiB | full vCPU | unlimited |

**Headroom**: orders of magnitude. The agent could run 10× more
aggressively before any budget is challenged.

---

## 4. Tracking commands for 48h soak

### One-line continuous monitor (recommended for operator)

```bash
ssh root@72.61.173.89 'bash -s' <<'EOF'
echo "─── $(date -u +%H:%M:%S) ───"
docker stats market-terminal --no-stream --format "  mem: {{.MemUsage}}  cpu: {{.CPUPerc}}  pids: {{.PIDs}}"
echo "  restarts: $(docker inspect market-terminal --format '{{.RestartCount}}')"
echo "  bus events:news:news.raw length: $(docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli XLEN events:news:news.raw)"
echo "  agent ticks (last 1h):"
docker logs --since 1h market-terminal 2>&1 | grep -c "news_agent_tick_complete"
echo "  agent errors (last 1h):"
docker logs --since 1h market-terminal 2>&1 | grep "agent.news.news.fetch" | grep -i error | head -3
EOF
```

Run this hourly during the 48h soak. Watch for trends.

### Aggregated 24h report

```bash
ssh root@72.61.173.89 'bash -s' <<'EOF'
echo "=== 24h Agent Resource Profile ==="
echo "  current memory: $(docker stats market-terminal --no-stream --format '{{.MemUsage}}')"
echo "  current pids: $(docker stats market-terminal --no-stream --format '{{.PIDs}}')"
echo "  restarts: $(docker inspect market-terminal --format '{{.RestartCount}}')"
echo ""
echo "  agent ticks total: $(docker logs --since 24h market-terminal 2>&1 | grep -c news_agent_tick_complete)"
echo "  agent ticks expected: ~720 (every 120s)"
echo ""
echo "  agent errors total: $(docker logs --since 24h market-terminal 2>&1 | grep 'agent.news.news.fetch' | grep -i error | wc -l)"
echo ""
echo "  Redis used_memory: $(docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli INFO memory | grep used_memory_human)"
echo "  Bus stream depth: $(docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli XLEN events:news:news.raw)"
EOF
```

---

## 5. Resource alert thresholds

Rollback the flag (Level 1 — env-only) if ANY of these trip:

| Metric | Threshold | Severity |
|---|---|---|
| Container memory growth in 24h | > 200 MiB attributable to flag | high |
| Sustained CPU > 50% for >10min | new pattern post-flag-flip | high |
| Restart count > 0 | any | critical |
| Agent `consecutive_failures` ≥ 5 | DISABLED state | high |
| News fetch latency p99 > 10s | sustained for ≥1h | medium |
| Bus stream depth growing without bound | indicates consumer lag (n/a in Stage 4.3) | medium |
| Redis used_memory > 100 MiB | unexpected pressure | medium |
| Any new ERROR from agent.news.news.fetch logger | non-transient | high |

Rollback recipe:
```bash
ssh root@72.61.173.89 'sed -i "/^AGENT_NEWS_FETCH_ENABLED=/d" /opt/zyvora/.env && docker compose -f /opt/zyvora/docker-compose.prod.yml up -d --force-recreate market-terminal'
```

---

## 6. Stage 4.3 specific observations to capture

Things uniquely worth measuring while the agent is the SOLE addition:

| Observation | How |
|---|---|
| First-tick boot lag | `docker logs market-terminal | grep "news_agent_tick_complete" | head -1` — should appear at t+120s |
| Tick interval drift | gather all timestamps; deltas should be 120s ± 5s |
| Cache contention | look for sustained latency_ms spikes on every Nth tick (where N = digest:agent ratio) |
| asyncio.to_thread overhead | latency_ms - (latency for direct call) ≈ thread-pool dispatch overhead, usually < 5ms |
| Event-loop blocking | if 200ms `/health` latency spikes appear after each tick, the agent is blocking |
| Memory leak | plot memory over 24h; should plateau, not climb |

---

## 7. Forecast: post-soak budget

If the 48h soak completes within budget, post-soak resource forecast:

| Resource | Stage 4.3 (1 agent) | Sprint 4 complete (1 agent + 1 critic) | Sprint 5 (5 news agents) |
|---|---|---|---|
| Memory | +30 MiB | +50 MiB | +150 MiB |
| CPU | +0.5% | +1% | +5% |
| Network | +100 MB/24h | +100 MB/24h | +500 MB/24h |
| Redis depth | 5K events | 5-10K events | 25-50K events |
| Redis memory | ~5 MB | ~10 MB | ~30 MB |

All comfortably within container limits.

---

## 8. Honest scope

This document is the **resource budget + monitoring playbook**. Actual
numbers will be filled in by the operator at hour-by-hour and daily
checkpoints. The pre-flag-flip baseline above is the only empirical
data; everything else is forecast based on:
- Unit test latency observations (10 tests in `test_sprint4_news_fetch_agent.py`)
- Stage A 15-min observation (Stage A — empty orchestrator)
- Architecture cost model (`MULTI_AGENT_PLAN.md §8`)

The first real data points become available at flip-time + 5 minutes.
