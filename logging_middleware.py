"""Sprint 2 Phase A — ASGI middleware for request correlation + access logging.

Provides RequestContextMiddleware which:
  1. Extracts X-Request-ID from inbound headers, or generates a fresh one.
  2. Sets request_id_var in ContextVar (async-safe propagation).
  3. Tracks request latency.
  4. Injects X-Request-ID into the response headers (for clients / downstream
     tracing).
  5. Emits a structured "request_complete" log line at end-of-request.

Disabled by setting LOG_HTTP_REQUESTS=false (the log line is suppressed;
context-var and header injection still happen — those are cheap and useful).

Out of scope for Sprint 2 (deferred to OBSERVABILITY_PLAN.md):
  - Prometheus histogram emission per request
  - OpenTelemetry span creation
  - Per-tenant request metering
"""

from __future__ import annotations

import logging
import os
import time

from logging_config import new_request_id, request_id_var


_log = logging.getLogger("http.request")

# Header constants (lower-case to match ASGI byte-key convention).
_HEADER_REQUEST_ID = b"x-request-id"

# Env-var driven toggle for the per-request log line. Header + context still
# work when this is off — only the log emission is suppressed.
_LOG_HTTP_REQUESTS_DEFAULT = "true"


class RequestContextMiddleware:
    """Pure-ASGI middleware. Avoids the Starlette BaseHTTPMiddleware overhead.

    Placement: add LAST (outermost) so request_id is set before any inner
    middleware logs anything. With FastAPI:
        app.add_middleware(CORSMiddleware, ...)
        app.add_middleware(RequestContextMiddleware)   # ← add last
    """

    def __init__(self, app):
        self.app = app
        # Re-read on every request so flipping the env var doesn't require
        # a restart. The cost is one os.environ.get per request — negligible.
        # Module-level cache could be added if profiling shows it's hot.

    async def __call__(self, scope, receive, send):
        # Only process HTTP scopes. WebSocket and lifespan pass through unchanged
        # (websocket correlation will be added in Sprint 3 alongside the agent
        # framework — it has different semantics: long-lived, per-connection
        # rather than per-request).
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _extract_or_generate_id(scope)
        token = request_id_var.set(request_id)

        start = time.perf_counter()
        # Mutable closure so send_wrapper can stash the response status.
        response_status: list[int] = [0]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response_status[0] = message["status"]
                # Inject the header. Note: copy the list to avoid mutating
                # any caller-owned reference.
                headers = list(message.get("headers", []))
                headers.append((_HEADER_REQUEST_ID, request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Let the exception propagate; FastAPI's exception handler will
            # produce a 500 response. Just make sure we log it with context.
            duration_ms = (time.perf_counter() - start) * 1000.0
            if _should_log():
                _log.exception(
                    "request_failed",
                    extra={
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "duration_ms": round(duration_ms, 2),
                    },
                )
            request_id_var.reset(token)
            raise
        else:
            duration_ms = (time.perf_counter() - start) * 1000.0
            if _should_log():
                _log.info(
                    "request_complete",
                    extra={
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": response_status[0],
                        "duration_ms": round(duration_ms, 2),
                    },
                )
            request_id_var.reset(token)


def _extract_or_generate_id(scope) -> str:
    """Look for X-Request-ID in the request; mint a new one if absent."""
    for key, value in scope.get("headers", ()):
        if key == _HEADER_REQUEST_ID:
            try:
                return value.decode("ascii")
            except UnicodeDecodeError:
                # Caller sent non-ASCII; mint a fresh one rather than trust.
                break
    return new_request_id()


def _should_log() -> bool:
    return os.environ.get("LOG_HTTP_REQUESTS", _LOG_HTTP_REQUESTS_DEFAULT).lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
