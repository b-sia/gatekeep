#!/bin/bash
set -e

# Helper script to mint a test API key through the service layer and print a
# ready-to-use curl example.
# Usage: bash scripts/init-test-key.sh [account-name] [key-name]
#
# Examples:
#   bash scripts/init-test-key.sh                 # account 'test-account', key 'test-key'
#   bash scripts/init-test-key.sh my-acct my-key

ACCOUNT_NAME="${1:-test-account}"
KEY_NAME="${2:-test-key}"

if ! command -v python3 &> /dev/null; then
    echo "❌ Error: python3 not found"
    exit 1
fi

# Ensure gatekeep is importable (alembic env.py and the ORM both need it).
echo "📦 Checking if gatekeep package is installed..."
if ! python3 -c "import gatekeep" 2>/dev/null; then
    echo "⚠️  gatekeep package not installed. Installing in editable mode..."
    if ! pip install -e . >/dev/null 2>&1; then
        echo "❌ Error: failed to install gatekeep package"
        exit 1
    fi
fi

# Bring the schema up to date (no-op if already at head).
echo "🔍 Applying migrations (if any)..."
alembic upgrade head

echo "🔑 Minting API key for account '$ACCOUNT_NAME'..."
RAW_KEY=$(python3 scripts/create_key.py "$ACCOUNT_NAME" "$KEY_NAME")

# Get default model from environment, .env file, or fallback
if [ -z "$DEFAULT_MODEL" ] && [ -f .env ]; then
    DEFAULT_MODEL=$(grep "^DEFAULT_MODEL=" .env | cut -d= -f2)
fi
DEFAULT_MODEL="${DEFAULT_MODEL:-claude-sonnet-5}"

echo ""
echo "✅ Test API key created successfully!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Account:  $ACCOUNT_NAME"
echo "Key Name: $KEY_NAME"
echo "Raw Key:  $RAW_KEY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "💡 Use this in curl requests:"
echo ""
echo "curl -X POST http://localhost:8100/v1/chat/completions \\"
echo "  -H \"Authorization: Bearer $RAW_KEY\" \\"
echo "  -H \"Content-Type: application/json\" \\"
echo "  -d '{\"model\": \"$DEFAULT_MODEL\", \"messages\": [{\"role\": \"user\", \"content\": \"hi\"}]}'"
echo ""
