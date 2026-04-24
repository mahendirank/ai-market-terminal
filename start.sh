#!/bin/zsh
# Start AI Market Terminal — survives terminal close
cd "$(dirname "$0")"

# Kill any existing instance on port 8001
lsof -ti:8001 | xargs kill -9 2>/dev/null
sleep 1

# Load .env if it exists
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

echo "Starting AI Market Terminal..."
nohup python3 run.py > server.log 2>&1 &
PID=$!
echo "Server PID: $PID"
echo "Logs: ~/ai-system/core/server.log"
echo "Dashboard: http://localhost:8001"

sleep 4
if curl -s http://localhost:8001/health > /dev/null 2>&1; then
    echo "✓ Server is running (PID $PID)"
    echo "✓ GROQ: $([ -n "$GROQ_API_KEY" ] && echo 'key loaded' || echo 'NOT SET')"
    echo "✓ TAVILY: $([ -n "$TAVILY_API_KEY" ] && echo 'key loaded' || echo 'NOT SET')"
else
    echo "✗ Server failed to start — check server.log"
    tail -20 server.log
fi
