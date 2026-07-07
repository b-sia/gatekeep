#!/bin/bash
set -e

# Helper script to generate and insert a test API key into the database.
# Usage: bash scripts/init-test-key.sh [key-name] [delete-existing]
#
# Examples:
#   bash scripts/init-test-key.sh                    # Create 'test-key'
#   bash scripts/init-test-key.sh my-key             # Create 'my-key'
#   bash scripts/init-test-key.sh my-key true        # Delete and recreate 'my-key'

KEY_NAME="${1:-test-key}"
DELETE_EXISTING="${2:-false}"

# Check if required tools are available
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: python3 not found"
    exit 1
fi

if ! command -v psql &> /dev/null; then
    echo "❌ Error: psql not found"
    exit 1
fi

# Check if api_keys table exists; if not, run migrations
echo "🔍 Checking database schema..."
TABLE_EXISTS=$(PGPASSWORD=gatekeep psql -U gatekeep -h localhost -d gatekeep -t -c "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'api_keys');" 2>/dev/null || echo "f")
if [ "$TABLE_EXISTS" != "t" ]; then
    echo "⚠️  api_keys table not found. Running migrations..."
    if ! alembic upgrade head; then
        echo "❌ Error: alembic migration failed"
        exit 1
    fi
fi

# Generate raw key and hash
echo "🔑 Generating API key..."
RAW_KEY=$(python3 -c "from gatekeep.auth_keys import generate_key; print(generate_key())")
HASH=$(python3 -c "from gatekeep.auth_keys import hash_key; print(hash_key('$RAW_KEY'))")

# Delete existing key if requested
if [ "$DELETE_EXISTING" = "true" ]; then
    echo "🗑️  Deleting existing key '$KEY_NAME'..."
    PGPASSWORD=gatekeep psql -U gatekeep -h localhost -d gatekeep -c "DELETE FROM api_keys WHERE name = '$KEY_NAME';" 2>/dev/null || true
fi

# Insert into database
echo "💾 Inserting into database..."
PGPASSWORD=gatekeep psql -U gatekeep -h localhost -d gatekeep << EOF
INSERT INTO api_keys (name, key_hash, active)
VALUES ('$KEY_NAME', '$HASH', true);
EOF

# Display results
echo ""
echo "✅ Test API key created successfully!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Key Name:   $KEY_NAME"
echo "Raw Key:    $RAW_KEY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "💡 Use this in curl requests:"
echo ""
echo "curl -X POST http://localhost:8100/v1/chat/completions \\"
echo "  -H \"Authorization: Bearer $RAW_KEY\" \\"
echo "  -H \"Content-Type: application/json\" \\"
echo "  -d '{\"model\": \"gpt-4\", \"messages\": [{\"role\": \"user\", \"content\": \"hi\"}]}'"
echo ""
