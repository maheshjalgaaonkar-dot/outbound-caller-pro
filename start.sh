#!/bin/bash
set -e

echo "=== Starting Aiona Voice ==="

echo ">>> Launching LiveKit agent worker in background..."
python agent.py start &
AGENT_PID=$!
echo ">>> Agent PID: $AGENT_PID"

echo ">>> Launching FastAPI server..."
exec uvicorn api:app --host 0.0.0.0 --port 8000 --workers 1
