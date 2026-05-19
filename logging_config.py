"""Sprint 2 Phase A — Centralized logging configuration.

Single source of truth for log setup. Provides:
  - setup_logging()  — idempotent dictConfig wrapper; call once at startup
  - JsonFormatter    — one-line JSON per record, OTel-compatible field names
  - ConsoleFormatter — human-readable text with context prefix
  - ContextFilter    — copies async-safe ContextVars onto every LogRecord
  - ContextVars      — request_id, tenant_id, trace_id, agent_name
  - ErrorCategory    — enum-like constants for structured error tagging
  - new_request_id() — generate a 12-char hex correlation ID

Design constraints (per Sprint 2 plan):
  - Import-time safe: no I/O, no env reads, no side effects on import.
  - Idempotent: setup_logging() can be called many times; only the first wins.
  - Async-safe: ContextVar is the only primitive used for cross-task state.
  - Future-compatible: trace_id_var ready for OpenTelemetry; agent_name_var
    ready for the Sprint 3 BaseAgent contract; LogRecord shape stable so
    consumers can be built before any shipper is chosen.
"""

from __future__ import annotations

import json
import logging
import logging.config
import os
import sys
import time
import uuid
from contextvars import ContextVar


# ──────────────────────────────────────────────────────────────────────────
# Context variables (async-safe — propagated across await automatically)
# ──────────────────────────────────────────────────────────────────────────

# Set by RequestContextMiddleware on every HTTP request.
# Set by future BaseAgent.run_once at the start of each agent tick.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Set by route handlers after auth resolves the tenant.
tenant_id_var: ContextVar[str] = ContextVar("tenant_id", default="-")

# Reserved for OpenTelemetry. Today the JsonFormatter emits "-".
# When OTel lands (Sprint 4+), an OTel processor will populate this from
# opentelemetry.trace.get_current_span().get_span_context().trace_id.
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")

# Reserved for Sprint 3 multi-agent work. BaseAgent.run_once sets this
# at the start of every tick. Downstream LLM / DB / external calls will
# inherit it via ContextVar propagation, no manual threading required.
agent_name_var: ContextVar[str] = ContextVar("agent_name", default="-")


# ──────────────────────────────────────────────────────────────────────────
# Error category constants — structured tagging for error log records
# ──────────────────────────────────────────────────────────────────────────

class ErrorCategory:
    """Constants used in `extra={"error_category": ErrorCategory.X}`.

    Sprint 2: just defined; not enforced. Sprint 3+ wires these into
    Prometheus metrics (errors_total{category=...}) and uses them for
    alert routing.
    """

    EXTERNAL_API = "external_api"   # yfinance, NSE, Groq, Anthropic, Telegram
    DATABASE = "database"           # SQLite locked / Redis OOM / connection refused
    VALIDATION = "validation"       # bad caller input
    INTERNAL = "internal"           # our bugs / unexpected exceptions
    TIMEOUT = "timeout"             # hit a deadline
    RATE_LIMIT = "rate_limit"       # external rate limit hit
    AUTH = "auth"                   # session / token errors
    CIRCUIT_OPEN = "circuit_open"   # external dep guarded by open circuit


# ──────────────────────────────────────────────────────────────────────────
# Formatters
# ──────────────────────────────────────────────────────────────────────────

# Fields the Python logging LogRecord populates by default. We never copy
# these into the JSON `extra` block — they have explicit positions in the
# JSON envelope or are intentionally dropped.
_LOGRECORD_BUILTIN_FIELDS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
        # Our ContextFilter additions — also already in top-level envelope.
        "request_id", "tenant_id", "trace_id", "agent",
    }
)


class JsonFormatter(logging.Formatter):
    """One-line JSON per record.

    Envelope shape (stable contract for downstream consumers):
        ts          : ISO-8601 UTC with ms (e.g. 2026-05-18T14:23:45.123Z)
        level       : INFO / WARNING / ERROR / DEBUG / CRITICAL
        logger      : record.name (e.g. "http.request", "core.alert_engine")
        msg         : formatted message string
        request_id  : from ContextVar, "-" if unset
        tenant_id   : from ContextVar
        trace_id    : from ContextVar (reserved for OTel)
        agent       : from ContextVar (reserved for Sprint 3)
        ...extras   : anything passed via log.info(..., extra={...})
        exc_type    : present only if record.exc_info set
        exc_msg     : ditto
        exc_traceback : multi-line string; consumers should treat as text

    Unserializable extra values fall back to repr() rather than failing
    the log call — never trade an outage for a log line.
    """

    def format(self, record: logging.LogRecord) -> str:
        envelope: dict = {
            "ts": _iso_utc(record.created, record.msecs),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
            "tenant_id": getattr(record, "tenant_id", tenant_id_var.get()),
            "trace_id": getattr(record, "trace_id", trace_id_var.get()),
            "agent": getattr(record, "agent", agent_name_var.get()),
        }

        # Copy through any "extra=" fields that weren't built-ins.
        for k, v in record.__dict__.items():
            if k in _LOGRECORD_BUILTIN_FIELDS or k in envelope:
                continue
            envelope[k] = _safe_json_value(v)

        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            envelope["exc_type"] = exc_type.__name__ if exc_type else None
            envelope["exc_msg"] = str(exc_value) if exc_value else None
            envelope["exc_traceback"] = self.formatException(record.exc_info)

        try:
            return json.dumps(envelope, default=str, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            # Final fallback: a minimal envelope describing the failure.
            return json.dumps(
                {
                    "ts": envelope["ts"],
                    "level": "ERROR",
                    "logger": "logging_config",
                    "msg": f"failed to serialize log record: {e!r}",
                    "original_logger": record.name,
                },
                ensure_ascii=False,
            )


class ContextFilter(logging.Filter):
    """Attach ContextVar values to every LogRecord.

    Required for the console formatter (which references %(request_id)s in
    its format string). The JSON formatter reads ContextVars directly, but
    we attach to the record anyway so external handlers see them too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.tenant_id = tenant_id_var.get()
        record.trace_id = trace_id_var.get()
        record.agent = agent_name_var.get()
        return True


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _iso_utc(created: float, msecs: float) -> str:
    """2026-05-18T14:23:45.123Z — millisecond precision, no timezone offset."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created)) + f".{int(msecs):03d}Z"


def _safe_json_value(v):
    """Return v if json-serializable, else its repr()."""
    try:
        json.dumps(v, default=str)
        return v
    except (TypeError, ValueError):
        return repr(v)


def new_request_id() -> str:
    """12-char hex correlation ID (96 bits). Shorter than full UUID for log readability."""
    return uuid.uuid4().hex[:12]


# ──────────────────────────────────────────────────────────────────────────
# Setup (idempotent)
# ──────────────────────────────────────────────────────────────────────────

# Module-level sentinel so repeated imports / calls don't double-configure.
_SETUP_DONE = False


def setup_logging(
    level: str | None = None,
    log_format: str | None = None,
) -> None:
    """Configure root + uvicorn loggers. Idempotent.

    Call exactly once at process startup, BEFORE any module that calls
    logging.getLogger() reads its own level. Subsequent calls are no-ops.

    Reads from env (each can be overridden by argument):
      LOG_LEVEL          INFO (default) / DEBUG / WARNING / ERROR
      LOG_FORMAT         console (default) / json
      UVICORN_ACCESS_LOG off (default) / on  — when "on", uvicorn.access
                         keeps its own per-request line; otherwise silenced
                         since RequestContextMiddleware logs the same info
                         in structured form.
    """
    global _SETUP_DONE
    if _SETUP_DONE:
        return

    level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    log_format = (log_format or os.environ.get("LOG_FORMAT", "console")).lower()
    uvicorn_access = os.environ.get("UVICORN_ACCESS_LOG", "off").lower()

    formatter_name = "json" if log_format == "json" else "console"
    uvicorn_access_level = level if uvicorn_access == "on" else "WARNING"

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    # __name__ resolves whether this module is imported as
                    # "logging_config" (script context) or "core.logging_config"
                    # (future package context).
                    "()": f"{__name__}.JsonFormatter",
                },
                "console": {
                    "format": (
                        "%(asctime)s %(levelname)-7s "
                        "[%(name)s] [%(request_id)s] %(message)s"
                    ),
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
            },
            "filters": {
                "context": {"()": f"{__name__}.ContextFilter"},
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                    "formatter": formatter_name,
                    "filters": ["context"],
                },
            },
            "root": {"level": level, "handlers": ["stdout"]},
            "loggers": {
                "uvicorn":        {"level": level, "propagate": True},
                "uvicorn.access": {"level": uvicorn_access_level, "propagate": True},
                "uvicorn.error":  {"level": level, "propagate": True},
            },
        }
    )

    _SETUP_DONE = True


def reset_for_testing() -> None:
    """Test-only helper: clears the idempotency sentinel so setup_logging
    can be called fresh in the next test. NOT for production use."""
    global _SETUP_DONE
    _SETUP_DONE = False
