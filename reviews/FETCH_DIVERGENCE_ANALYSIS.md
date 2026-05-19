# FETCH_DIVERGENCE_ANALYSIS.md

> How to detect + reason about divergence between the legacy news
> pipeline and the new `NewsFetchAgent` during the 48h dual-run.

---

## 0. The fundamental theorem of this dual-run

**Both code paths call the SAME function** (`news.get_all_news()`).
That function has a **30s in-process cache** shared between callers.

Consequence: data divergence between legacy and agent is **structurally
impossible** when calls happen within 30s of each other. When calls
happen > 30s apart, the difference reflects ONLY:
1. New headlines that arrived between fetches
2. Transient errors that affect one call but not another

This is why the dual-run is more about **infrastructure verification**
than **data verification**.

---

## 1. What we CAN measure for divergence

### A. Tick-to-tick stability (within the agent's own emissions)

Each `news.raw` event includes:
- `count` — number of headlines
- `source_count` — distinct sources
- `sources` (top 20)
- `latency_ms`

Comparing consecutive events from the agent reveals:
- Article count delta (`count_delta` in the agent's log)
- Source set changes (`sources_added`, `sources_removed`)
- Latency volatility (`latency_delta_ms`)

This is the **drift detection** built into the agent.

### B. Agent latency vs. legacy execution time

Legacy `_async_digest_loop` doesn't emit a structured `latency_ms`, but
it prints `[DIGEST]` lines that bracket the digest cycle. By
correlating timestamps:

```bash
# Extract legacy digest cycle durations
ssh root@72.61.173.89 'docker logs --since 24h market-terminal' \
  | grep -E "DIGEST" \
  | python3 -c "
import sys, re
from datetime import datetime
events = []
for line in sys.stdin:
    m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) .*\[DIGEST\] (.+)', line)
    if m:
        events.append((datetime.fromisoformat(m.group(1)), m.group(2)))
# Compute spans (subtract consecutive timestamps)
for i in range(1, len(events)):
    dt = (events[i][0] - events[i-1][0]).total_seconds()
    print(f'{events[i][1]}: {dt:.1f}s since previous')
"
```

Then compare with agent's `news_agent_tick_complete` `latency_ms`:
- Agent fetch (single get_all_news call): typically 100ms cached, 1-3s uncached
- Legacy digest cycle (much more than just news): much longer; not directly comparable

### C. Article count overlap

When the agent ticks at t and the legacy digest runs at t-Δ (where Δ < 30s), they share the cached result. Counts must be EQUAL.

When Δ > 30s, agent count and legacy count may differ by 0-5 articles (new headlines in the window). A difference of >10 is suspicious.

To verify equality during cache-share windows: take a snapshot from the legacy log shortly after an agent emission and confirm count matches.

### D. Source consistency

Sources expected to appear: per `news.SOURCE_CATEGORY` table — about 30+ named sources. The agent's `source_count` should hover around 15-25 (not all sources have recent items at any given time).

If `source_count` drops below 5 for sustained periods: a fetch is failing silently, returning partial data.

---

## 2. What we CANNOT measure (and why it doesn't matter)

| Wanted comparison | Why infeasible | Why it's fine |
|---|---|---|
| "Are the agent's articles bit-identical to legacy's?" | Same function call → trivially yes | No bug to detect |
| "Does the agent's parsing differ?" | Same parser → trivially no | No bug to detect |
| "Does the agent's source list differ?" | Same source mapping in news.py | No bug to detect |
| "Does the agent fail when legacy succeeds (or vice versa)?" | Possible if call timing diverges + transient flake hits one but not the other | RetryPolicy + circuit breaker (Sprint 4.5) absorbs this |

---

## 3. Divergence-detection automation (future)

For Sprint 5+, when the agent might start using a SEPARATE fetch path
(not the legacy `news.get_all_news`), a real divergence detector becomes valuable. Sketch:

```python
# Sprint 5+ — divergence_detector.py
class DivergenceDetectorAgent(StreamAgent):
    """Consumes news.raw events. For each, queries the legacy cache and
    computes diff. Logs if divergence > threshold."""

    stream = "events:news:raw"
    consumer_group = "divergence.detector"

    async def handle_event(self, envelope):
        agent_count = envelope.payload["count"]
        agent_sources = set(envelope.payload["sources"])
        # Query legacy in-process state
        legacy_news = await asyncio.to_thread(get_all_news)  # cache hit
        legacy_count = len(legacy_news)
        legacy_sources = {n.get("source") for n in legacy_news if isinstance(n, dict)}
        delta = legacy_count - agent_count
        source_diff = agent_sources ^ legacy_sources
        if abs(delta) > 5 or len(source_diff) > 3:
            self.log.warning("divergence_detected", extra={...})
```

**Not implemented in Sprint 4.3** because:
- Same cache → no divergence to detect (would always log 0 diff)
- Adds complexity
- Stage 4.4 critic infrastructure will provide a better home for this

---

## 4. Empirical analysis recipe (operator runs during 48h dual-run)

### Daily summary

```bash
ssh root@72.61.173.89 'bash -s' <<'EOF'
echo "=== Agent tick stats (last 24h) ==="
docker logs --since 24h market-terminal 2>&1 \
  | grep news_agent_tick_complete \
  | python3 -c "
import sys, re
counts, lats = [], []
for line in sys.stdin:
    if m := re.search(r'count=(\d+)', line):
        counts.append(int(m.group(1)))
    if m := re.search(r'latency_ms=([\d.]+)', line):
        lats.append(float(m.group(1)))
if counts:
    print(f'  ticks: {len(counts)}')
    print(f'  count: min={min(counts)} median={sorted(counts)[len(counts)//2]} max={max(counts)}')
if lats:
    print(f'  latency_ms: min={min(lats):.1f} median={sorted(lats)[len(lats)//2]:.1f} max={max(lats):.1f}')
"

echo
echo "=== Legacy digest emissions (last 24h) ==="
docker logs --since 24h market-terminal 2>&1 | grep -c "DIGEST"

echo
echo "=== Any agent ERROR lines? ==="
docker logs --since 24h market-terminal 2>&1 \
  | grep -E "agent.news.news.fetch.*ERROR" | head -5 || echo "  (none)"
EOF
```

### Expected daily numbers

| Metric | Healthy range |
|---|---|
| Agent ticks/24h | 720 ± 5 (every 120s) |
| Agent fetch latency p50 | < 500ms |
| Agent fetch latency p99 | < 3000ms |
| Agent count min..max | within ±10 of each other (modulo big news events) |
| Legacy digest count/24h | ~288 (every 5 min) |
| Agent ERROR lines | 0-2 (transient OK) |
| `consecutive_failures` peak | < 3 |

If any metric is far outside the range, investigate before flipping further flags.

---

## 5. Critical observation: the agent IS the legacy fetch

Stage 4.3 is really about validating the **agent runtime mechanics**
under real production fetch behavior, not about validating the fetch
itself.

Things being validated:
- `asyncio.to_thread` works correctly for the blocking `get_all_news`
- Timeout cancellation behaves sanely
- Retry policy doesn't retry-storm on transient failures
- Bus emission round-trips
- Per-tick logging shape works
- Orchestrator manages the agent's lifecycle correctly

Things being NOT validated (because they're identical to legacy):
- News parsing
- RSS feed reliability
- Source coverage

When Stage 4.3 passes its 48h soak, we have proven the **agent runtime
is production-grade** for one agent. Sprint 5+ can then add more
agents with confidence.

---

## 6. Reporting cadence

| When | What | Where |
|---|---|---|
| Flag-flip day | Boot logs confirm registration | This file's §1 |
| t+1h | First hourly snapshot | Append to this file or a daily log |
| t+12h | Halfway summary | Add to NEWS_AGENT_DUAL_RUN_REPORT.md §5 |
| t+24h | Full daily summary + decision | New section here |
| t+48h | Cutover-or-rollback decision | Drives Stage 4.4 readiness |
