import os
import uvicorn

port = int(os.environ.get("PORT", 8001))
print(f"Starting on port {port}")
uvicorn.run("dashboard_api:app", host="0.0.0.0", port=port, log_level="info")
