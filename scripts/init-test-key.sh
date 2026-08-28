#!/bin/bash
set -e

# Helper script to mint a test API key through the service layer and print a
# ready-to-use curl example.
# Usage: bash scripts/init-test-key.sh [--operator] [--email <address>] [account-name] [key-name]
#
# Pass --operator to grant the account fleet-wide operator access. This is the
# bootstrap path for the first operator on a fresh database - the dashboard's
# prompt/eval/account routes require operator access and nothing in-product can
# promote the very first account.
#
# Pass --email <address> to also set a dashboard login password (prompted
# interactively). Without it the account has an API key but no dashboard
# login - for --operator specifically, that means the operator can
# administer the fleet via the API but can't open the dashboard UI at all,
# since paste-a-key login was retired in favor of email/password sessions.
#
# Examples:
#   bash scripts/init-test-key.sh                                  # account 'test-account', key 'test-key'
#   bash scripts/init-test-key.sh my-acct my-key
#   bash scripts/init-test-key.sh --operator                       # first operator, API-only
#   bash scripts/init-test-key.sh --operator --email you@x.com     # first operator, with dashboard login

OPERATOR_FLAG=""
EMAIL_FLAG=""
POSITIONAL=()
while [ $# -gt 0 ]; do
    case "$1" in
        --operator)
            OPERATOR_FLAG="--operator"
            shift
            ;;
        --email)
            EMAIL_FLAG="--email $2"
            shift 2
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done

ACCOUNT_NAME="${POSITIONAL[0]:-test-account}"
KEY_NAME="${POSITIONAL[1]:-test-key}"

if ! command -v python3 &> /dev/null; then
    echo "❌ Error: python3 not found"
    exit 1
fi

# Ensure gatekeep is importable (alembic env.py and the ORM both need it).
# Run the check from a neutral cwd (/): a bare `python3 -c "import gatekeep"`
# from the repo root imports the local source tree via cwd-on-sys.path and
# passes even when the *installed* editable package is broken or points at a
# stale worktree - which is exactly how a dangling install silently slips
# through until `alembic` (a console script, no cwd trick) fails to import it.
echo "📦 Checking if gatekeep package is installed..."
if ! (cd / && python3 -c "import gatekeep") 2>/dev/null; then
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
RAW_KEY=$(python3 scripts/create_key.py $OPERATOR_FLAG $EMAIL_FLAG "$ACCOUNT_NAME" "$KEY_NAME")

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
