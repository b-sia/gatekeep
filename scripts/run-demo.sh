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
if ! bash scripts/init-test-key.sh test-key false > /dev/null 2>&1; then
    echo "Creating new test key..."
    bash scripts/init-test-key.sh test-key true
else
    echo "✅ Test key already exists"
fi

# Extract the API key from the previous init script output
API_KEY=$(python3 -c "from gatekeep.auth_keys import generate_key, hash_key; key = generate_key(); print(key)" 2>/dev/null || echo "sk-test-key")

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
