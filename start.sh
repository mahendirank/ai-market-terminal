#!/bin/zsh
# Start AI Market Terminal — survives terminal close
cd "$(dirname "$0")"

# Kill any existing instance on port 8001
lsof -ti:8001 | xargs kill -9 2>/dev/null

echo "Starting AI Market Terminal..."
nohup python3 dashboard_api.py > server.log 2>&1 &
PID=$!
echo "Server PID: $PID"
echo "Logs: ~/ai-system/core/server.log"
echo "Dashboard: http://localhost:8001"

# Wait a moment and confirm it started
sleep 3
if kill -0 $PID 2>/dev/null; then
    echo "✓ Server is running (PID $PID)"
else
    echo "✗ Server failed to start — check server.log"
fi
