#!/bin/bash
set -e

# Helper script to run the complete Gatekeep + Demo setup
# This starts both the gateway and demo app.
# Requires GATEKEEP_API_KEY to already be set in .env - run
# scripts/init-test-key.sh first if you don't have a key yet.

echo "🚀 Starting Gatekeep Demo Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check if Gatekeep is already running
if ! curl -s http://localhost:8100/health > /dev/null 2>&1; then
    echo "⚠️  Gatekeep server not running on port 8100"
    echo "Starting Gatekeep via docker-compose..."
    docker-compose up -d
    echo "⏳ Waiting for Gatekeep to be ready..."
    sleep 5
else
    echo "✅ Gatekeep server is running"
fi

# Make sure a GATEKEEP_API_KEY is configured before starting the demo
if [ -f .env ]; then
    GATEKEEP_API_KEY=$(grep "^GATEKEEP_API_KEY=" .env | cut -d= -f2)
fi
if [ -z "$GATEKEEP_API_KEY" ]; then
    echo "❌ Error: GATEKEEP_API_KEY is not set in .env"
    echo "   Run: bash scripts/init-test-key.sh"
    exit 1
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Setup Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "🌐 Gatekeep Gateway:"
echo "   http://localhost:8100"
echo ""
echo "💬 Demo Chat App:"
echo "   http://localhost:8200"
echo ""
echo "🔑 Using API Key from .env:"
echo "   $GATEKEEP_API_KEY"
echo ""

# Check if demo dependencies are installed
if ! python3 -c "import fastapi, httpx" 2>/dev/null; then
    echo "⚠️  Installing dependencies..."
    pip install -e . > /dev/null
fi

echo "Starting Demo Chat Application..."
echo ""
python3 -m demo.app 2>&1 | sed 's/^/  [demo] /'
