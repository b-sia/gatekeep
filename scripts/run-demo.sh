#!/bin/bash
set -e

# Helper script to run the complete Gatekeep + Demo setup
# This starts both the gateway and demo app, and initializes a test key

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

# Initialize test key if needed
echo ""
echo "🔑 Initializing API key..."
INIT_OUTPUT=$(bash scripts/init-test-key.sh test-key true)
echo "$INIT_OUTPUT" | grep -v "^Raw Key:"

# Extract the actual API key that init-test-key.sh inserted into the database
API_KEY=$(echo "$INIT_OUTPUT" | sed -n 's/^Raw Key: *//p')

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
echo "🔑 Test API Key:"
echo "   $API_KEY"
echo ""

# Check if demo dependencies are installed
if ! python3 -c "import fastapi, httpx" 2>/dev/null; then
    echo "⚠️  Installing dependencies..."
    pip install -e . > /dev/null
fi

echo "Starting Demo Chat Application..."
echo ""
python3 -m demo.app 2>&1 | sed 's/^/  [demo] /'
