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

port = int(os.environ.get("PORT", 8001))

print(f"=== AI Market Terminal starting on port {port} ===")
print(f"Python: {sys.version}")
print(f"Railway env: {os.environ.get('RAILWAY_ENVIRONMENT', 'LOCAL')}")
print(f"GROQ key set: {'yes' if os.environ.get('GROQ_API_KEY') else 'NO'}")
print(f"Telegram token set: {'yes' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'using default'}")

uvicorn.run(
    "dashboard_api:app",
    host="0.0.0.0",
    port=port,
    log_level="info",
    access_log=True,
)
