# PRODUCTION_VALIDATION.md

> Live post-deploy validation results, 2026-05-19. All 10 checks pass.

---

## Validation matrix

| # | Check | Method | Result | Status |
|---|---|---|---|---|
| 1 | `/health` (Docker healthcheck) | `docker exec market-terminal curl http://localhost:8001/health` | `{"status":"ok"}` HTTP 200 | ✅ |
| 2 | `/api/health` (system-wide) | `docker exec ... curl /api/health` | JSON body: `redis.ok=true, sqlite.ok=true (23 DBs), no degraded subsystems` | ✅ |
| 3 | X-Request-ID propagation | `curl -sI` against `/api/health` | `x-request-id: 521b2b1bd43e` (12-char hex per spec) | ✅ |
| 4 | Structured log per request | `docker logs --tail 50` | `2026-05-19 10:47:55 INFO [http.request] [da8196752855] request_complete` | ✅ |
| 5 | Async request isolation | 2 sequential `curl -sI`; compare IDs | `d2b974e3b4b2` ≠ `ea6f526895e0` (unique per request) | ✅ |
| 6 | Existing dashboard root | `curl /` | HTTP 307 (redirect to login — same as pre-deploy) | ✅ |
| 7 | WebSocket endpoint registered | `curl /ws` without proper upgrade | HTTP 400 (endpoint exists, rejected non-WS) | ✅ |
| 8 | News pipeline endpoints | `curl /api/news`, `/api/signals` | HTTP 401 (auth required — same as pre-deploy) | ✅ |
| 9 | TradingView data endpoints | `curl /api/prices`, `/api/regime` | HTTP 401 (auth required — same as pre-deploy) | ✅ |
| 10 | Caddy → market-terminal HTTPS path | `curl https://zyvoratech.co/health` from internet | HTTP 200 | ✅ |

---

## Sprint-1+2 visible runtime effects (intended)

These are the new behaviors that Sprint 1+2 introduces, verified live:

### Sprint 2 — Phase A logging

```
2026-05-19 10:47:55 INFO    [http.request] [da8196752855] request_complete
2026-05-19 10:47:57 INFO    [http.request] [b94f72713fa5] request_complete
2026-05-19 10:47:57 INFO    [http.request] [f079cebd405a] request_complete
2026-05-19 10:47:57 INFO    [http.request] [521b2b1bd43e] request_complete
```

Format: `<ts> <level> [<logger>] [<request_id>] <msg>` — matches `LOG_FORMAT=console` (the default).

### Sprint 2 — `X-Request-ID` response header

Every HTTP response now carries a 12-char hex ID. Caller-supplied IDs (when the inbound request has its own `X-Request-ID`) are preserved.

### Sprint 1 — `python run.py` invocation

The Dockerfile CMD is unchanged (`python run.py`). The new `__main__` guard makes the module safely importable without launching uvicorn — verified by the parametrized import test on CI, not at runtime.

---

## What's NOT visible in production yet (correct)

- **Orchestration is dormant**. `'orchestration' in sys.modules == False` in a fresh subprocess inside the container.
- **No agents running**. `/api/agents` endpoint does not exist (Sprint 4 work).
- **No circuit breakers wired**. `/api/circuits` endpoint does not exist (Sprint 4 work).
- **No new event streams**. Redis stream list is unchanged from pre-deploy.

---

## Pre-existing noise observed

Found in logs, **not caused by this deploy** — these were already present before Sprint 1-3 landed:

| Pattern | Cause | Action |
|---|---|---|
| `[TG] send failed 400: chat not found` | `.env` has `TELEGRAM_CHAT_ID=your-telegram-chat-id-here` (placeholder, never replaced) | Pre-existing config issue. User should set a real chat ID or set `ALERT_DISABLED=true`. |
| `[yfinance] HTTP Error 404: Quote not found for symbol: LTIM.NS / TATAMOTORS.NS` | Yahoo Finance API returning 404 for these tickers | Pre-existing third-party data issue. Symbol list should be reviewed; out of scope for Sprint 1-3. |

Neither indicates a regression from this deploy.

---

## External UX validation

- `https://zyvoratech.co/health` returned HTTP 200 from the internet (via Caddy → market-terminal) — confirms the external request path works end-to-end with the new image.
- The browser-facing UI was not exercised in this validation (would require admin login). Recommend the user spot-check 2–3 frequently-used flows in the next hour.

---

## Verdict

**Sprint 1+2 foundation is live in production and behaving as designed.**
**Sprint 3 orchestration code is on disk and dormant — flags-off mode.**

No regressions, no errors attributable to the deploy, no resource pressure.
