# THREAD_LEAK_INCIDENT.md — Rollback Evidence

> Incident during Sprint 4 Stage 4.4 deploy, 2026-05-20. Agent disabled
> per the instability protocol. **Root cause: legacy news.py — NOT the
> Stage 4.4 code, NOT the SignalCriticAgent.**

---

## 1. Timeline (UTC, 2026-05-20)

| Time | Event |
|---|---|
| ~07:51 | Stage 4.4 code deploy begins. Pre-deploy: old container ~20.7h uptime, healthy, `news.fetch` had 589 ticks, PIDs stable. |
| 07:53:01 | Container force-recreated with Stage 4.4 image. `signal.critic` flag OFF; `news.fetch` flag ON. |
| 07:53:35 | Deploy script "Resource snapshot": **PIDs = 14** (normal). Boot looked clean. |
| ~07:56 | Post-deploy state grab: **PIDs = 152** — far above the 13-21 norm. |
| ~07:57 | Investigation: PIDs = 174, then 172. Thread dump: ~170 threads on pid 1, all "python", stuck in `futex_wait_queue` + `do_poll`. Only **1** `news_agent_tick_complete` logged in 6 min → agent's `run_once` was hanging. |
| ~07:58 | **ACTION: disabled `AGENT_NEWS_FETCH_ENABLED=false`, force-recreated container** (per instability protocol). |
| 07:56:09* | Fresh container started, agent OFF (`agent_registered_and_started` count = 0). |
| ~07:57–08:00 | Post-rollback container ALSO churned threads: 15 → 115 → 164 → 284 → 135 (sawtooth). **Confirms the agent was NOT the cause.** |
| ~08:01–08:06 | Sawtooth settled: 176 → 44 → 47 → 43 → 41 → 62. |
| ~08:07 | Steady-state: **39-42 threads, ~432 MiB, stable.** 29 threads in `do_poll` (network-stuck). Container healthy, 0 restarts. |

\* The two restart timestamps overlap because the disable+restart happened seconds after the investigation; container clock shows 07:56:09 for the final restart.

---

## 2. Symptom

Thread (PID) count on the `market-terminal` container climbed from the
normal 13-21 range to a peak of **284**, with elevated memory. Thread
kernel stacks showed the majority stuck in:
- `do_poll` — blocked on a network socket poll (RSS feed reads)
- `futex_wait_queue` — blocked on a lock / idle pool workers

Only **1** successful `news_agent_tick_complete` was logged in the
first 6 minutes (the agent ticks every 120s — should have been ~3).
The agent's `run_once` was **hanging** inside the news fetch.

---

## 3. Root cause

### The chain of causation

1. **`news.py` fans out across ~66 RSS feeds** using a
   `concurrent.futures.ThreadPoolExecutor` (`get_rss_news`).
2. **One or more RSS feeds is hanging today** (2026-05-20) — a socket
   read that does not complete. (`requests` has `FEED_TIMEOUT=7s`, but a
   feed that trickles bytes slowly can evade the read timeout, OR the
   hang is in `feedparser.parse` / connection setup.)
3. **A hung feed leaves its `ThreadPoolExecutor` worker thread alive**
   indefinitely.
4. **Cold-cache boot amplifies it**: a freshly-restarted container has
   an empty 30s news cache. The legacy `_warm`, `_continuous_refresh`,
   and `_async_digest_loop` threads all hammer `get_all_news()` in the
   first minutes — each a cache MISS → full 66-feed fan-out → each hits
   the hung feed → orphans threads.
5. **The Stage 4.3 `NewsFetchAgent` design flaw amplifies it further**:
   `run_once` does `await asyncio.to_thread(get_all_news)` wrapped in
   `asyncio.wait_for(timeout=30)`. When the 30s timeout fires, it
   cancels the **async wrapper** — but **Python cannot kill the OS
   thread** that `asyncio.to_thread` spawned. The underlying
   `get_all_news` call (and its 66 pool workers) keeps running. Each
   timed-out agent tick orphans a fresh batch of threads.

### Why Stage 4.3 ran 20h clean but today leaked

The Stage 4.3 NewsFetchAgent ran from 2026-05-19 11:13 for ~20.7h with
PIDs stable at 12-21. The difference:

- **Yesterday the feeds were healthy.** Fetches completed within
  timeout; `to_thread` threads finished and were reclaimed.
- **The old container had a permanently-warm cache** for ~20h — most
  `get_all_news` calls were cache HITS (no fetch, no threads).
- **Today a feed is hung** AND **the Stage 4.4 deploy forced a
  container restart** → cold cache → aggressive cold fetches → hit the
  hung feed → thread pile-up.

**Any container restart today — Stage 4.4 or not — would have triggered
this.** It is a latent legacy bug exposed by an environmental trigger,
not a regression introduced by Stage 4.4.

---

## 4. What was ruled OUT

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Stage 4.4 SignalCriticAgent caused it | **RULED OUT** | `signal.critic` flag was OFF the entire time; `agent_registered_and_started` count never included it |
| Stage 4.4 code (even unused) caused it | **RULED OUT** | After disabling `news.fetch`, the fresh container still churned threads with 0 agents registered |
| NewsFetchAgent was the sole cause | **RULED OUT** | Leak persisted after the agent was disabled — legacy `news.py` reproduces it on cold-cache boot |
| Unbounded leak → imminent OOM/crash | **RULED OUT** | Thread count is a sawtooth (peak 284) that **settled to a stable ~40** once the cache warmed; container never restarted, stayed healthy |

---

## 5. What was CONFIRMED

| Finding | Evidence |
|---|---|
| Legacy `news.py` has a latent thread-bounding weakness | Cold-cache boot with agent OFF still produced the churn |
| Stage 4.3 `NewsFetchAgent` has a real design flaw | `asyncio.to_thread` + `wait_for` timeout cannot kill the OS thread — confirmed by Python semantics + the 1-tick-then-hang behavior |
| The condition self-stabilizes | Once the cache warms (~8-10 min), cold fetches stop; thread count plateaus at ~40 |
| ~29 threads remain genuinely stuck | `do_poll` wchan count = 29 — orphaned sockets on the hung feed; they clear on next restart |
| Production stayed up throughout | `restart=0`, `health=healthy`, `/health`=200, `zyvoratech.co/health`=200 the entire time |

---

## 6. Current production state (post-incident)

```
.env:   AGENT_ORCHESTRATOR_ENABLED=true
        AGENT_NEWS_FETCH_ENABLED=false   ← disabled during incident
        AGENT_SIGNAL_CRITIC_ENABLED      ← not set (never enabled)

Container: healthy, 0 restarts
Threads:   ~40 (stable; 29 in do_poll from the hung feed, will clear on next restart)
Memory:    ~432 MiB (elevated vs ~330 norm; cache + orphan-thread stacks)
Agents:    0 registered (orchestrator on, idle — equivalent to Stage A)
Legacy:    fully authoritative, serving traffic normally
```

This is effectively the **Stage A baseline** (orchestrator on, zero
agents) — a state proven stable for 24h+ earlier. The elevated thread
count is the residue of the incident and will reset on the next
container restart.

---

## 7. Rollback actions taken

| Step | Command | Result |
|---|---|---|
| 1. Disable agent | `sed -i 's/^AGENT_NEWS_FETCH_ENABLED=.*/AGENT_NEWS_FETCH_ENABLED=false/' .env` | flag off |
| 2. Restart | `docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal` | fresh container, agent not registered |
| 3. Verify | thread count settled ~40; health green; legacy serving | stable |

**Legacy pipeline was preserved throughout** — it was never disabled
(and could not be, since it IS the production news path).

---

## 8. Required fixes before re-enabling NewsFetchAgent

The `news.fetch` agent must STAY DISABLED until BOTH of these land:

### Fix A — NewsFetchAgent must not orphan threads on timeout

The agent's `run_once` wraps a blocking call in `asyncio.to_thread` +
`asyncio.wait_for`. A timeout cancels the coroutine but not the thread.

Options:
- Run `get_all_news` in a **dedicated `ProcessPoolExecutor`** — a
  process CAN be killed on timeout (heavier, but bulletproof).
- Add a **hard per-feed deadline inside news.py** so the underlying
  fetch always returns (see Fix B — this fixes both).
- Have the agent call a **non-fanning, single-flight cache-read-only**
  variant of `get_all_news` that never triggers a fetch — the agent
  becomes a pure cache observer, and the legacy digest remains the only
  fetch trigger.

The third option is the cleanest for "shadow mode": the agent observes
the cache, never fetches. Recommended.

### Fix B — news.py feed fetch must hard-bound hung feeds

`get_rss_news`'s `ThreadPoolExecutor` should:
- Use `executor.map(..., timeout=N)` or `as_completed(..., timeout=N)`
  so a hung feed doesn't block the whole batch.
- Better: wrap each feed fetch with a deadline that GUARANTEES the
  worker thread returns (e.g. a session with strict connect+read
  timeouts, or drop slow feeds via a per-feed circuit breaker).
- Identify and quarantine the currently-hung feed (check
  `sources_config.py` against live feed health).

**Fix B is legacy code** — modifying it needs its own change-controlled
task, since the user's standing rule is "don't modify legacy business
logic." The thread-bounding is arguably a safety fix, not a logic
change, but it should be scoped explicitly.

---

## 9. Stage 4.4 status — UNAFFECTED

To be unambiguous: **Stage 4.4 (SignalCriticAgent) is complete and
sound.**

- 27 unit tests pass; CI green (330 tests on PR #12)
- `sim_signal_critic.py` 6/6 scenarios pass
- Code is deployed to the VPS (flag OFF)
- The agent was NEVER enabled and is NOT implicated in this incident
- `SignalCriticAgent` does not import or call `news.py` — it consumes
  `events:signal:candidate`, which has no producer yet

Stage 4.4 can be enabled independently and safely (see
`SAFE_ENABLEMENT_PLAN.md`). The incident is entirely about the news
fetch path.

---

## 10. Lessons

1. **`asyncio.to_thread` + `asyncio.wait_for` is a thread-leak trap.**
   A timeout that "cancels" a `to_thread` call leaves the OS thread
   running. Any agent wrapping a blocking call this way needs a real
   kill mechanism (process pool) or a guaranteed-to-return blocking
   call.

2. **Cold-cache restarts expose latent fan-out bugs.** A warm cache
   masked this for as long as the production container stayed up. The
   bug was always there.

3. **The instability protocol worked as a procedure** even though the
   first hypothesis (agent = cause) was wrong. Disable → observe →
   the observation itself produced the correct diagnosis.

4. **Environmental triggers matter.** "It worked yesterday" is not
   "it works." A single hung RSS feed turned a latent bug into an
   incident.
