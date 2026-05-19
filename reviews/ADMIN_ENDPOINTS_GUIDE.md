# ADMIN_ENDPOINTS_GUIDE.md

> How to use the 3 admin endpoints added in Sprint 4 Stage 4.1.

---

## 1. The three endpoints

| Path | Method | Auth | Purpose |
|---|---|---|---|
| `/api/agents` | GET | session cookie | Registered agents + their health |
| `/api/circuits` | GET | session cookie | Circuit breaker state for every wrapped external service |
| `/api/streams/health` | GET | session cookie | Length of each known Redis Stream |

All three are gated by the existing `AuthMiddleware` (same auth used for the dashboard). Calling without credentials returns **HTTP 401**.

---

## 2. Response shapes

### `/api/agents`

**Flag OFF (default)**:
```json
{ "enabled": false, "agents": [] }
```

**Flag ON, 0 agents (Stage 4.1)**:
```json
{ "enabled": true, "agents": [] }
```

**Flag ON, agents registered (Stage 4.3+)**:
```json
{
  "enabled": true,
  "agents": [
    {
      "name": "news.fetch",
      "status": "running",
      "family": "news",
      "version": "v1",
      "total_ticks": 47,
      "total_failures": 0,
      "consecutive_failures": 0,
      "last_tick_success": true,
      "last_tick_age_s": 12.3,
      "tick_interval": 60.0
    }
  ]
}
```

**Possible status values**: `registered`, `running`, `stopping`, `stopped`, `disabled`.

### `/api/circuits`

**Always available** (the default registry is process-global, exists regardless of flag):

```json
{
  "circuits": [
    {
      "service": "groq",
      "state": "closed",
      "consecutive_failures": 0,
      "opened_at": 0.0,
      "failure_threshold": 5,
      "recovery_timeout": 30.0
    },
    {
      "service": "telegram",
      "state": "open",
      "consecutive_failures": 12,
      "opened_at": 1684471800.5,
      "failure_threshold": 10,
      "recovery_timeout": 10.0
    }
  ]
}
```

State values: `closed`, `open`, `half_open`.

### `/api/streams/health`

**Flag OFF**:
```json
{ "enabled": false, "streams": [] }
```

**Flag ON**:
```json
{
  "enabled": true,
  "streams": [
    { "stream": "events:news:raw",         "length": 0 },
    { "stream": "events:signal:candidate", "length": 0 },
    { "stream": "dlq:news:raw",            "length": 0 },
    { "stream": "dlq:signal:candidate",    "length": 0 }
  ]
}
```

`length: -1` means the stream couldn't be queried (e.g. Redis briefly unreachable). Treat as "unknown", not "empty".

---

## 3. Example usage (operator's curl recipes)

### From a logged-in browser session

Once you've logged into `https://zyvoratech.co` as admin, open a new tab and just navigate to:
- `https://zyvoratech.co/api/agents`
- `https://zyvoratech.co/api/circuits`
- `https://zyvoratech.co/api/streams/health`

The session cookie is sent automatically.

### From the command line (admin only)

```bash
# Step 1: Login + capture cookie
ADMIN_PASS="<your-admin-password>"
curl -c /tmp/zyvora-cookies.txt -X POST https://zyvoratech.co/login \
  -d "username=admin&password=${ADMIN_PASS}" -L -o /dev/null

# Step 2: Hit endpoints with the cookie
curl -b /tmp/zyvora-cookies.txt https://zyvoratech.co/api/agents | jq .
curl -b /tmp/zyvora-cookies.txt https://zyvoratech.co/api/circuits | jq .
curl -b /tmp/zyvora-cookies.txt https://zyvoratech.co/api/streams/health | jq .

# Cleanup
rm /tmp/zyvora-cookies.txt
```

**Important**: never commit `/tmp/zyvora-cookies.txt` or the admin password. The session cookie is short-lived but should still be treated as a secret.

### From inside the container (no auth)

```bash
docker exec market-terminal curl -s http://localhost:8001/api/agents | jq .
```

This **does NOT bypass auth** — you'll still get 401. (Confirming the AuthMiddleware applies regardless of origin.)

To bypass auth for debugging only, you'd need to temporarily disable AuthMiddleware in code — out of scope for routine operations.

---

## 4. When to check each endpoint

### `/api/agents`
- Daily: confirm registered agents are RUNNING (not DISABLED).
- After a deploy: confirm new agents picked up the new code.
- After an incident: see which agent's `consecutive_failures` is non-zero.
- For drift: monitor `last_tick_age_s` — should be ≤ `tick_interval × 2`.

### `/api/circuits`
- Daily: confirm no service is stuck OPEN.
- After provider-side incidents (Groq outage, Telegram issue): expect to see `open` transition.
- When troubleshooting alert delivery: check if `telegram` breaker is open.

### `/api/streams/health`
- Daily: confirm DLQ streams are empty.
- After a deploy: confirm event flow continues (`events:news:raw.length` should be growing if producer is healthy).
- When debugging consumer lag: rising `length` on a stream with a known consumer suggests the consumer is falling behind.

---

## 5. Alerting thresholds (recommended for Sprint 5+ when metrics endpoint lands)

These are the alerting rules I'd recommend wiring into Prometheus alertmanager in Sprint 5:

| Rule | Threshold | Severity |
|---|---|---|
| Any agent DISABLED | `status="disabled"` | warning |
| Agent stuck | `last_tick_age_s > 3 × tick_interval` | warning |
| Agent failing | `consecutive_failures > 2` | warning |
| Any circuit OPEN > 10min | `state="open"` AND `now - opened_at > 600` | critical |
| Circuit flapping | rate of state changes > 5/hour | warning |
| DLQ depth > 50 | `dlq:* .length > 50` | warning |
| Stream backlog | `events:* .length > 0.8 × max_len` | warning |

Until those land, manual periodic checks are the alerting mechanism.

---

## 6. What's intentionally absent

| Feature | Why |
|---|---|
| `POST /api/agents/{name}/start` and `/stop` | No agents to manage in Sprint 4.1. Will add when agents register. |
| `POST /api/agents/{name}/reset` | Same — needed when first DISABLED agent appears (Stage 4.3+). |
| `POST /api/circuits/{service}/force-close` | Defensive only; risk of papering over a real issue. Add manually when needed. |
| `GET /api/dlq/{stream}` browsing | Sprint 5+ when DLQ has real content. For now: `docker exec redis-cli XRANGE dlq:... - + COUNT 10`. |
| Pagination | All collections are bounded; not needed until 50+ agents. |
| WebSocket streaming of state | Polling is fine at current scale; revisit when /api/agents traffic >1 req/s. |

---

## 7. Implementation notes

- All three endpoints are **stateless**: they read snapshots from `app.state` and the global circuit registry; no DB calls.
- Each endpoint responds in **< 100ms** (verified by `test_admin_endpoints_response_under_100ms`).
- Errors inside the snapshot functions are caught and returned as `{..., "error": <ExcName>}`. The HTTP status stays 200 — the contract is "always JSON, never 5xx".
- Auth-gating is enforced by the existing `AuthMiddleware`. Sprint 4 didn't add a new middleware layer.

---

## 8. Versioning

The response shapes are a **stable contract**. Sprint 5+ may ADD fields (e.g. `last_error_type` to agents), but will not rename or remove fields. Consumers can rely on `enabled`, `agents`, `circuits`, `streams` keys to remain.
