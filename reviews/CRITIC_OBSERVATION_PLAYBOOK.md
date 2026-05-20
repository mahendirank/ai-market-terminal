# CRITIC_OBSERVATION_PLAYBOOK.md

> How to observe `SignalCriticAgent` when it is enabled. Forward-looking
> — the agent is currently deployed flag-OFF and idle (no producer for
> `events:signal:candidate` until Sprint 5).

---

## 0. When this playbook applies

This playbook is for the future moment when:
- `AGENT_SIGNAL_CRITIC_ENABLED=true` is flipped, AND
- A producer for `events:signal:candidate` exists (Sprint 5+)

In Sprint 4.4, neither is true — the agent is dormant. This document is
the operational guide for when it goes live.

**Do not enable the critic as part of Sprint 4.** It would tick against
an empty stream and produce nothing. Enabling it is a Sprint 5 action,
gated on a candidate-event producer existing.

---

## 1. Enabling the critic (Sprint 5+)

```bash
ssh root@72.61.173.89 'bash -s' <<'EOF'
cd /opt/zyvora
# Pre-flip snapshot
STAMP="2026-XX-XX_$(date -u +%H%M)"
VOL=$(docker volume inspect zyvora_terminal_db --format "{{.Mountpoint}}")
tar czf /opt/backups/db-pre-critic-${STAMP}.tar.gz -C "$VOL" .
# Flip
echo "AGENT_SIGNAL_CRITIC_ENABLED=true" >> .env
docker compose -f docker-compose.prod.yml up -d --force-recreate market-terminal
EOF
```

Boot logs should show TWO `agent_registered_and_started` lines (one for
`news.fetch` if that's also on, one for `signal.critic`).

---

## 2. What to observe

### A. Verdict log lines

Each consumed candidate emits one `signal_critic_observed`:

```
INFO [agent.signal.signal.critic] [<request_id>] signal_critic_observed
  verdict=accept|reject
  reason=<chain_ok | missing_asset | below_confidence_floor | stale_event | ...>
  confidence=<0.0-1.0>
  asset=<symbol>
  envelope_confidence=<the signal's own confidence>
```

### B. Critique events on the bus

```bash
ssh root@72.61.173.89 'docker compose -f /opt/zyvora/docker-compose.prod.yml exec -T redis redis-cli XLEN events:signal:signal.critique'
```

Each critique event payload (metadata only):
```json
{
  "original_event_type": "signal.candidate",
  "original_trace_id": "...",
  "verdict": "accept" | "reject",
  "reason": "...",
  "confidence": 0.0-1.0,
  "markers": [...],
  "observe_only": true,
  "enforced": false
}
```

### C. Verdict-rate analysis

```bash
ssh root@72.61.173.89 'docker logs --since 24h market-terminal' \
  | grep signal_critic_observed \
  | grep -oE 'verdict=[a-z]+' | sort | uniq -c
```

Healthy expectation: mostly `accept`. A high `reject` rate means
either the upstream signal producer is emitting bad candidates, OR the
critic thresholds are mis-tuned. Investigate before any thought of
enforcement.

### D. Reject reason breakdown

```bash
ssh root@72.61.173.89 'docker logs --since 24h market-terminal' \
  | grep signal_critic_observed \
  | grep -oE 'reason=[a-z_]+' | sort | uniq -c | sort -rn
```

This is the key dataset for the eventual enforce-mode decision.

---

## 3. Stop conditions (rollback the critic if any trip)

| Condition | Threshold |
|---|---|
| `signal_critic_chain_exception_fail_open` log lines | > 0 (any) — a critic bug |
| `signal_critic_emit_failed_fail_open` | sustained (bus problem) |
| Critic adds measurable latency to anything | any (it shouldn't — it's a separate consumer) |
| Agent `consecutive_failures` ≥ 3 | DISABLED — investigate |
| Memory growth attributable to the critic | > 30 MiB |
| Thread count growth attributable to the critic | > 5 |

The critic is observe-only, so its failure can NEVER block a signal.
The stop conditions above are about the critic's own health, not
production risk.

### Rollback (30s)
```bash
ssh root@72.61.173.89 'sed -i "s/^AGENT_SIGNAL_CRITIC_ENABLED=.*/AGENT_SIGNAL_CRITIC_ENABLED=false/" /opt/zyvora/.env && docker compose -f /opt/zyvora/docker-compose.prod.yml up -d --force-recreate market-terminal'
```

---

## 4. The 24h observe-soak checklist (before ANY enforcement is considered)

Enforcement (rejecting bad signals, DLQ-routing them) is **explicitly
out of scope for Sprint 4 AND Sprint 5's first half**. Before
enforcement is even discussed, the critic must run in observe mode for
≥24h with a real producer, and ALL of these must hold:

- [ ] Critic ran ≥24h with `events:signal:candidate` actively produced
- [ ] Zero `signal_critic_chain_exception_fail_open` lines
- [ ] Zero `signal_critic_emit_failed_fail_open` lines
- [ ] Verdict distribution computed: accept% vs reject%
- [ ] Reject-reason breakdown computed and reviewed by a human
- [ ] No "obviously good" signal was rejected (manual spot-check of a
      sample of `reject` verdicts)
- [ ] False-positive rate estimated < 5%
- [ ] Critic added 0 restarts, < 30 MiB memory, < 5 threads
- [ ] `signal.critique` stream depth growing as expected (one per candidate)
- [ ] No latency regression on the signal-producing path

Only when every box is ✅ does an "enforce mode" design discussion
begin — and enforce mode would itself be a new flag, a new sprint, and
its own soak.

---

## 5. The thread-leak lesson applied to the critic

The Stage 4.4 deploy incident (`THREAD_LEAK_INCIDENT.md`) was caused by
`asyncio.to_thread` + timeout orphaning threads in the NEWS path.

**The SignalCriticAgent does NOT have this risk:**
- It's a `StreamAgent` — `run_once` reads ONE event from the bus and
  dispatches to `handle_event`.
- `handle_event` runs the critic chain, which is **pure async, no
  `asyncio.to_thread`, no blocking I/O fan-out**.
- The critic chain is deterministic and fast (< 10ms).
- Bus operations are async (`redis.asyncio`).

So the critic agent has no thread-orphan exposure. The deterministic
critics (Schema, ConfidenceFloor, RecentBar) do no network I/O at all.

This is verified by `sim_signal_critic.py` and the 27 unit tests.

---

## 6. Critic enablement is decoupled from the news incident

The news thread-leak incident does NOT block enabling the critic:
- Different code path (`signal.critic` vs `news.fetch`)
- Different failure surface (no `to_thread`, no fan-out)
- The critic doesn't import or call `news.py`

However: **there is no operational reason to enable the critic in
Sprint 4** — without a producer it does nothing. Enable it in Sprint 5
alongside the candidate-event producer.
