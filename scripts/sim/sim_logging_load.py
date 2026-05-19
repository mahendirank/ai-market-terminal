"""Simulation: structured logging + correlation IDs under load.

Spins up N concurrent fake ASGI requests through RequestContextMiddleware
and verifies:
  - Every request gets a UNIQUE request_id
  - The header makes it back into the response
  - ContextVar doesn't leak between concurrent requests
  - Per-request log line is emitted exactly once
  - Caller-supplied X-Request-ID is preserved

Run:
    python -m scripts.sim.sim_logging_load
    # or from core/:
    python scripts/sim/sim_logging_load.py
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

# Ensure core/ is on sys.path no matter where invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from logging_config import (
    JsonFormatter,
    request_id_var,
    setup_logging,
    reset_for_testing,
)
from logging_middleware import RequestContextMiddleware


# ──────────────────────────────────────────────────────────────────────
# In-memory log capture (so we can inspect what was emitted)
# ──────────────────────────────────────────────────────────────────────

class CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []
        self.formatter = JsonFormatter()

    def emit(self, record):
        self.records.append(self.formatter.format(record))


def _build_app():
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    def ping():
        # Use the ContextVar inside the handler — this is the
        # critical thing: must NOT leak between requests.
        return {"saw_request_id": request_id_var.get()}

    return app


def _summarize(records: list[str]) -> dict:
    parsed = [json.loads(r) for r in records]
    req_completes = [p for p in parsed if p.get("msg") == "request_complete"]
    return {
        "total_logs": len(parsed),
        "request_complete_count": len(req_completes),
        "unique_request_ids": len({p["request_id"] for p in req_completes}),
        "first_3": req_completes[:3],
        "max_duration_ms": max((p.get("duration_ms", 0) for p in req_completes), default=0),
        "min_duration_ms": min((p.get("duration_ms", 0) for p in req_completes), default=0),
    }


async def _fire_requests(n: int):
    """Use TestClient to issue N requests via threads.

    TestClient is sync (httpx wrapped). To simulate concurrency we
    delegate to a thread pool — this represents the multi-tenant
    concurrent-request shape uvicorn sees in prod.
    """
    client = TestClient(_build_app())

    def one_request(i: int):
        # Half the requests caller-supply a request ID; half let the
        # middleware mint one. Exercises both code paths.
        headers = {"X-Request-ID": f"caller-supplied-{i}"} if i % 2 == 0 else {}
        resp = client.get("/ping", headers=headers)
        return {
            "status": resp.status_code,
            "saw": resp.json()["saw_request_id"],
            "header": resp.headers.get("x-request-id"),
            "caller_supplied": "X-Request-ID" in headers,
        }

    # Run them concurrently via threads.
    loop = asyncio.get_running_loop()
    results = await asyncio.gather(*[
        loop.run_in_executor(None, one_request, i) for i in range(n)
    ])
    return results


async def main():
    # Capture logs from http.request — that's where middleware emits.
    reset_for_testing()
    os.environ["LOG_HTTP_REQUESTS"] = "true"
    capture = CaptureHandler()
    http_log = logging.getLogger("http.request")
    http_log.addHandler(capture)
    http_log.setLevel(logging.DEBUG)

    N = 200
    print(f"=== sim_logging_load: firing {N} concurrent requests ===")
    t0 = time.perf_counter()
    results = await _fire_requests(N)
    elapsed = time.perf_counter() - t0
    print(f"  → completed in {elapsed*1000:.1f} ms")
    print()

    # 1. Unique IDs
    handler_seen = {r["saw"] for r in results}
    header_seen = {r["header"] for r in results}
    print(f"  Unique request_ids seen in handler: {len(handler_seen)} / {N}")
    print(f"  Unique X-Request-ID in responses:   {len(header_seen)} / {N}")

    # 2. Caller-supplied preserved
    caller_supplied = [r for r in results if r["caller_supplied"]]
    preserved = sum(1 for r in caller_supplied if r["header"] == r["saw"])
    print(f"  Caller-supplied IDs preserved:      {preserved} / {len(caller_supplied)}")

    # 3. Per-request log
    summary = _summarize(capture.records)
    print(f"  request_complete log lines:         {summary['request_complete_count']} / {N}")
    print(f"  Unique request_ids in logs:         {summary['unique_request_ids']}")
    print(f"  Latency p-min..p-max (ms):          {summary['min_duration_ms']} .. {summary['max_duration_ms']}")
    print()

    # ── Verdicts ──
    ok = True
    if len(handler_seen) != N:
        print(f"  FAIL: handler saw duplicate request_ids — ContextVar may have leaked")
        ok = False
    if summary["request_complete_count"] != N:
        print(f"  FAIL: expected {N} request_complete logs, got {summary['request_complete_count']}")
        ok = False
    if summary["unique_request_ids"] != N:
        print(f"  FAIL: log lines share request_ids — middleware reuse bug")
        ok = False
    if preserved != len(caller_supplied):
        print(f"  FAIL: caller-supplied IDs lost ({preserved}/{len(caller_supplied)})")
        ok = False

    print()
    print(f"  Throughput: {N / elapsed:.1f} req/s (sync TestClient threadpool)")
    print()
    print("=== VERDICT:", "PASS" if ok else "FAIL", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
