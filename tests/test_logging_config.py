"""Sprint 2 Phase A — tests for core/logging_config.py.

Test isolation strategy: each test creates its own JsonFormatter / records
directly without calling setup_logging() (which has process-wide side
effects). The one idempotency test uses reset_for_testing() to clear the
sentinel between calls.
"""
import asyncio
import io
import json
import logging
import sys
from pathlib import Path

import pytest

# Ensure core/ is on sys.path so this test works under pytest from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logging_config import (
    ContextFilter,
    ErrorCategory,
    JsonFormatter,
    agent_name_var,
    new_request_id,
    request_id_var,
    reset_for_testing,
    setup_logging,
    tenant_id_var,
    trace_id_var,
)


# ──────────────────────────────────────────────────────────────────────
# JsonFormatter envelope shape
# ──────────────────────────────────────────────────────────────────────

def _make_record(msg="hello", level=logging.INFO, name="test.logger", extra=None):
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


@pytest.mark.smoke
def test_json_formatter_produces_valid_json():
    line = JsonFormatter().format(_make_record())
    parsed = json.loads(line)
    assert parsed["level"] == "INFO"
    assert parsed["msg"] == "hello"
    assert parsed["logger"] == "test.logger"
    # Stable envelope fields always present.
    for k in ("ts", "level", "logger", "msg", "request_id", "tenant_id", "trace_id", "agent"):
        assert k in parsed, f"missing envelope field: {k}"


@pytest.mark.smoke
def test_json_formatter_pulls_context_vars():
    token_r = request_id_var.set("req-abc")
    token_t = tenant_id_var.set("tenant-42")
    token_tr = trace_id_var.set("trace-xyz")
    token_a = agent_name_var.set("test.agent")
    try:
        parsed = json.loads(JsonFormatter().format(_make_record()))
        assert parsed["request_id"] == "req-abc"
        assert parsed["tenant_id"] == "tenant-42"
        assert parsed["trace_id"] == "trace-xyz"
        assert parsed["agent"] == "test.agent"
    finally:
        request_id_var.reset(token_r)
        tenant_id_var.reset(token_t)
        trace_id_var.reset(token_tr)
        agent_name_var.reset(token_a)


@pytest.mark.smoke
def test_json_formatter_includes_extra_fields():
    rec = _make_record(extra={"error_category": ErrorCategory.EXTERNAL_API, "url": "https://x.com"})
    parsed = json.loads(JsonFormatter().format(rec))
    assert parsed["error_category"] == "external_api"
    assert parsed["url"] == "https://x.com"


@pytest.mark.smoke
def test_json_formatter_handles_unserializable_extra():
    class NotSerializable:
        def __repr__(self):
            return "NotSerializable<token>"

    rec = _make_record(extra={"weird": NotSerializable()})
    parsed = json.loads(JsonFormatter().format(rec))
    assert parsed["weird"] == "NotSerializable<token>"


@pytest.mark.smoke
def test_json_formatter_renders_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        exc_info = sys.exc_info()
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "explosion", (), exc_info)
    parsed = json.loads(JsonFormatter().format(rec))
    assert parsed["exc_type"] == "ValueError"
    assert parsed["exc_msg"] == "boom"
    assert "Traceback" in parsed["exc_traceback"]


# ──────────────────────────────────────────────────────────────────────
# ContextFilter — populates record attributes for the console formatter
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
def test_context_filter_attaches_attrs():
    token = request_id_var.set("ctx-test")
    try:
        rec = _make_record()
        ContextFilter().filter(rec)
        assert rec.request_id == "ctx-test"
        assert rec.tenant_id == "-"
        assert rec.trace_id == "-"
        assert rec.agent == "-"
    finally:
        request_id_var.reset(token)


# ──────────────────────────────────────────────────────────────────────
# Async-safe context propagation
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
def test_context_var_isolates_across_asyncio_tasks():
    """request_id_var set in one task must NOT leak into a concurrent task
    that started before the set. This is the key safety property for
    serving many concurrent requests on one process."""

    async def task(name: str, expected_id: str):
        # Each task starts with default "-", then sets its own ID.
        assert request_id_var.get() == "-"
        request_id_var.set(f"id-{name}")
        await asyncio.sleep(0.001)  # yield, giving the OTHER task a chance to interfere
        assert request_id_var.get() == f"id-{name}", (
            f"task {name} saw {request_id_var.get()!r} after interleaving — leak!"
        )

    async def main():
        # Each create_task gets a fresh context copy → defaults restored.
        await asyncio.gather(task("A", "id-A"), task("B", "id-B"))

    asyncio.run(main())


# ──────────────────────────────────────────────────────────────────────
# setup_logging idempotency
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
def test_setup_logging_is_idempotent():
    reset_for_testing()
    setup_logging(level="DEBUG")
    handlers_before = list(logging.getLogger().handlers)
    setup_logging(level="ERROR")  # second call should be a no-op
    handlers_after = list(logging.getLogger().handlers)
    assert handlers_before == handlers_after, "setup_logging double-applied"


# ──────────────────────────────────────────────────────────────────────
# new_request_id shape
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
def test_new_request_id_is_12_char_hex():
    rid = new_request_id()
    assert len(rid) == 12
    assert all(c in "0123456789abcdef" for c in rid)
    # Two consecutive calls must differ.
    assert new_request_id() != rid


# ──────────────────────────────────────────────────────────────────────
# ErrorCategory constants stable contract
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.smoke
def test_error_category_constants_present():
    # If any of these are removed, downstream code searching for them breaks.
    for attr in (
        "EXTERNAL_API", "DATABASE", "VALIDATION", "INTERNAL",
        "TIMEOUT", "RATE_LIMIT", "AUTH", "CIRCUIT_OPEN",
    ):
        assert hasattr(ErrorCategory, attr), f"missing ErrorCategory.{attr}"
