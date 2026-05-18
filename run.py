import os
import sys
import uvicorn

# Load .env if present (local dev)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)
except ImportError:
    _env = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(_env):
        for line in open(_env):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

if __name__ == "__main__":
    # Sprint 2 Phase A — configure structured logging BEFORE importing
    # dashboard_api (which triggers SQLite / Redis init and module-level
    # loggers). setup_logging() is idempotent.
    from logging_config import setup_logging
    setup_logging()

    port = int(os.environ.get("PORT", 8001))

    print(f"=== AI Market Terminal starting on port {port} ===")
    print(f"Python: {sys.version}")
    print(f"Railway env: {os.environ.get('RAILWAY_ENVIRONMENT', 'LOCAL')}")
    print(f"GROQ key set: {'yes' if os.environ.get('GROQ_API_KEY') else 'NO'}")
    print(f"Telegram token set: {'yes' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'using default'}")

    # Uvicorn's access_log is silenced inside setup_logging when
    # UVICORN_ACCESS_LOG=off (default) because RequestContextMiddleware
    # emits the structured equivalent. Setting access_log=True here keeps
    # the option open via the env var — uvicorn won't re-create the logger,
    # only the level matters.
    uvicorn.run(
        "dashboard_api:app",
        host="0.0.0.0",
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        access_log=True,
    )
