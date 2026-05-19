# NEWS_AGENT_DUAL_RUN_REPORT.md

> Sprint 4 Stage 4.3 — NewsFetchAgent implemented, deployed code-only
> (flag OFF). Empirical dual-run data deferred to operator flag-flip.
> 2026-05-19.

---

## 0. State at report time

| Aspect | Value |
|---|---|
| Code merged to main | ✅ PR #9 → `dd6fddbf` |
| Code deployed to VPS | ✅ at 2026-05-19 16:19 UTC |
| `AGENT_ORCHESTRATOR_ENABLED` | `true` (kept on since Stage A) |
| `AGENT_NEWS_FETCH_ENABLED` | NOT SET → defaults to `false` |
| `agent_registered_and_started` log lines | **0** (correct: flag off) |
| Sprint 1-4.1 + 4.3 tests | 229 pass locally, 303 pass on CI |
| 12 simulations | 12/12 pass |

**The dual-run has NOT yet started.** Code is on disk; operator must
flip the flag when ready.

---

## 1. Agent specification (as built)

`orchestration/agents/news_fetch_agent.py`:

| Property | Value |
|---|---|
| name | `news.fetch` |
| family | `news` |
| version | `v1` |
| tick_interval | 120s (env `NEWS_FETCH_TICK_INTERVAL`) |
| timeout | 30s (env `NEWS_FETCH_TIMEOUT`) |
| retry_policy | `max_attempts=3, base_delay=2s, max_delay=10s, jitter=0.2, categories={external_api, timeout}` |
| input_critic | `AlwaysAcceptCritic` (no critic in Stage 4.3) |
| emit_event default stream | `events:news:news.raw` |

### Tick body
```python
async def run_once(self):
    from news import get_all_news
    t_start = time.perf_counter()
    news_list = await asyncio.to_thread(get_all_news)
    latency_ms = (time.perf_counter() - t_start) * 1000
    # Drift vs previous tick
    drift = self._compute_drift(count, sources, latency_ms)
    self.log.info("news_agent_tick_complete", extra={...})
    await self.emit_event(event_type="news.raw", payload={...})
```

### Bus emission payload (bounded, ~1 KB)
```json
{
  "count": 42,
  "latency_ms": 1247.83,
  "source_count": 18,
  "sources": ["Reuters", "CNBC", "Bloomberg", ...top 20...],
  "shadow_mode": true
}
```

**The full news list is NOT shipped through the bus** in Stage 4.3.
Reasoning: no consumer exists yet; metadata is enough to prove the
pipeline works. Sprint 5+ will include data once a real consumer
(e.g. `NewsDedupAgent`) needs it.

---

## 2. Dual-run topology

```
                    ┌────────────────────────────────┐
                    │  Legacy news pipeline          │
                    │  (UNCHANGED — authoritative)   │
                    │                                │
                    │  - _async_digest_loop (5 min)  │
                    │  - on-demand from routes       │
                    │  - prioritize_news + telegram  │
                    │  - signal_memory + alerts      │
                    │                                │
                    │  Output: existing UI/Telegram  │
                    └────────────┬───────────────────┘
                                 │
                          shared │ cache
                                 ▼
                    ┌────────────────────────────────┐
                    │  news.get_all_news()           │
                    │  - 30s in-process cache        │
                    │  - single-flight lock          │
                    └────────────┬───────────────────┘
                                 │
                                 │ (also called by)
                                 ▼
                    ┌────────────────────────────────┐
                    │  NewsFetchAgent (NEW)          │
                    │  - 120s tick                   │
                    │  - asyncio.to_thread wrap      │
                    │  - emit news.raw event         │
                    │  - log tick stats              │
                    │                                │
                    │  Output: Redis stream + logs   │
                    │          (NO consumers yet)    │
                    └────────────────────────────────┘
```

**Critical safety property**: agent's output goes to a stream NO ONE
reads. Failures are isolated; the legacy pipeline never sees them.

---

## 3. Cache cooperation analysis

`news.get_all_news()` has a 30s in-process cache. Both legacy callers
AND the agent share this cache. Effect:

| Caller | Cadence | Cache behavior |
|---|---|---|
| `_async_digest_loop` | every 300s | Each call is a cache MISS (300 > 30s TTL) |
| Route handlers (`/api/news/*`) | on-demand | Mostly cache HITS (within 30s of digest) |
| `NewsFetchAgent` | every 120s | Each call is a cache MISS (120 > 30s) |

Effective external fetch rate:
- Pre-Sprint-4.3: ~1 fetch per 300s from digest + occasional on-demand
- With agent enabled: ~1 fetch per 300s from digest + ~1 fetch per 120s from agent ≈ **+150% fetch rate**

Mitigation: agent's tick_interval is configurable via `NEWS_FETCH_TICK_INTERVAL`. If external API rate limits become an issue, raise to 180 or 300s.

**This is the ONLY non-zero cost of dual-run**: external API load. Memory, CPU, async tasks are all bounded.

---

## 4. Observation playbook (operator runs when ready)

### Pre-flip checklist
```bash
# 1. Confirm Stage A has been running cleanly for ≥24h
ssh root@72.61.173.89 'docker inspect market-terminal --format "{{.RestartCount}}"'
# Expected: 0

# 2. Fresh DB snapshot
ssh root@72.61.173.89 'bash -s' <<'EOF'
STAMP="2026-XX-XX_$(date -u +%H%M)"
VOL=$(docker volume inspect zyvora_terminal_db --format "{{.Mountpoint}}")
tar czf /opt/backups/db-pre-news-flag-${STAMP}.tar.gz -C "$VOL" .
EOF
```

### Flip the flag
```bash
ssh root@72.61.173.89 'bash -s' <<'EOF'
cd /opt/zyvora
echo "AGENT_NEWS_FETCH_ENABLED=true" >> .env
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal
# Wait for healthy
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 6
  s=$(docker inspect --format="{{.State.Health.Status}}" market-terminal)
  echo "  $i: $s"
  [ "$s" = "healthy" ] && break
done
# Verify agent registered
docker logs --tail 50 market-terminal | grep agent_registered_and_started
EOF
```

### Hourly checkpoint
```bash
# Per-tick stats from the agent's structured log
ssh root@72.61.173.89 'docker logs --since 1h market-terminal | grep news_agent_tick_complete' | head -20

# Memory + resource snapshot
ssh root@72.61.173.89 'docker stats market-terminal --no-stream --format "mem={{.MemUsage}} cpu={{.CPUPerc}} pids={{.PIDs}}"'

# Bus state
ssh root@72.61.173.89 'docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli XLEN events:news:news.raw'
# Expected: growing by ~30 events/hour (1 every 2 minutes); plateaus at MAXLEN 5000 after ~7 days

# /api/agents (authenticated)
# Browser: https://zyvoratech.co/api/agents → look for news.fetch
```

### Stop conditions (rollback if any are true)
- Agent `consecutive_failures` ≥ 3 in any 1h window
- Memory growth > 50 MiB/h sustained
- Restart count > 0
- Any new ERROR from `agent.news.news.fetch` logger (excluding ones already classified as transient by retry policy)
- Legacy `[NEWS]` log line frequency drops > 50% vs pre-flip baseline (suggests cache contention)
- External API 429 rate limit signals (yfinance / NSE / etc. starts complaining)

### Rollback (30s)
```bash
ssh root@72.61.173.89 'sed -i "/^AGENT_NEWS_FETCH_ENABLED=/d" /opt/zyvora/.env && docker compose -f /opt/zyvora/docker-compose.prod.yml up -d --force-recreate market-terminal'
```

---

## 5. Expected behavior when flag flips ON

Within the first 5 minutes:
- 1-2 `news_agent_tick_complete` log lines (depending on tick alignment)
- 1-2 `news.raw` events in the bus
- `agent_registered_and_started` log line at boot
- `/api/agents` shows `news.fetch` with `status: running`

Within the first hour:
- ~30 tick logs (every 120s)
- ~30 `news.raw` events
- Memory growth: < 30 MiB
- Latency per tick: typically 100-500ms (cache miss path); near zero on cache-hit paths (rare)

Within 24h:
- ~720 tick logs
- ~720 `news.raw` events (or plateau at MAXLEN 5000)
- Memory plateau under 500 MiB (well within budget)
- 0 or 1 transient failures expected (cache contention, brief network blip)
- 0 sustained failures expected

---

## 6. Reports linkage

- This file: empirical state + playbook
- `FETCH_DIVERGENCE_ANALYSIS.md`: methodology for comparing agent vs legacy outputs
- `AGENT_RESOURCE_PROFILE.md`: resource budget + tracking metrics
- `SHADOW_MODE_VALIDATION.md`: safety property verification
- `STAGE_4_4_READINESS.md`: gate criteria for Stage 4.4 (SignalCriticAgent)

---

## 7. Honest gap

This report is **forward-looking**. The agent's actual production
behavior under load — does it really return same counts as legacy?
does latency match? does memory really stay flat? — will be measurable
ONLY after the operator flips the flag and accumulates ≥24h of soak
data.

Until then, this is the **plan + the readiness assessment**. Empirical
results will populate the matrices in §5 once the soak begins.
