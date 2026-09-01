# Gatekeep dev command aliases. Install `just`: https://github.com/casey/just
#
# Note: no DOCKER_HOST override on this machine - plain docker/docker compose
# already targets Docker Desktop correctly (see memory: dev-environment-quirks).

default:
    @just --list

# --- Full stack ---

# Start the full stack (gateway + postgres + redis + ollama + prometheus + grafana)
up:
    docker compose up -d

# Stop and remove containers (keeps volumes)
down:
    docker compose down

# Stop and remove containers AND volumes (wipes the dev database)
down-clean:
    docker compose down -v

# Tail logs for all services, or one: `just logs gateway`
logs *service:
    docker compose logs -f {{service}}

# Show container status
ps:
    docker compose ps

# --- Gateway ---

# Rebuild the gateway image (needed after any backend code change - no bind mount)
build:
    docker compose build gateway

# Rebuild and restart just the gateway, picking up backend code changes
rebuild: build
    docker compose up -d gateway

# Restart the gateway container without rebuilding
restart:
    docker compose restart gateway

# Shell into the running gateway container
shell:
    docker compose exec gateway sh

# --- Dependencies only (for local pytest / dev servers) ---

# Start only postgres + redis, no gateway image build
up-deps:
    docker compose up -d postgres redis

# Shell into the postgres container (no psql on host - use this instead)
psql:
    docker compose exec postgres psql -U gatekeep -d gatekeep

# --- API keys ---

# Mint a test API key
init-key:
    bash scripts/init-test-key.sh

# Mint a test API key with a dashboard login (non-operator)
init-key-email email:
    bash scripts/init-test-key.sh --email {{email}}

# Bootstrap an operator account with a dashboard login
init-operator email:
    bash scripts/init-test-key.sh --operator --email {{email}}

# --- Dev database seeding ---

# Populate the dev DB with demo accounts, logins, keys, prompts, and history (idempotent; safe to re-run)
seed:
    python scripts/seed_dev.py

# Wipe the seed-owned tables and repopulate from scratch (use after nuking the DB)
seed-reset:
    python scripts/seed_dev.py --reset

# --- Testing & linting ---

# Run the Python test suite (needs TEST_DATABASE_URL + up-deps running)
test:
    pytest

# Run the dashboard test suite
test-dashboard:
    cd dashboard && npm test

# Lint (and format-check) Python sources
lint:
    ruff check .
    ruff format --check .

# Auto-fix lint issues and format Python sources
fmt:
    ruff check --fix .
    ruff format .

# --- Dashboard SPA ---

# Run the dashboard dev server (proxies /dashboard/api to :8100)
dashboard-dev:
    cd dashboard && npm run dev

# Build the dashboard SPA
dashboard-build:
    cd dashboard && npm run build

# --- Database migrations ---

# Apply pending Alembic migrations
migrate:
    alembic upgrade head

# Autogenerate a new migration from model changes: `just makemigration "add a column"`
makemigration message:
    alembic revision --autogenerate -m "{{message}}"

# --- Load testing (see loadtest/README.md) ---

# Bring up the full stack with the load-test override (stub provider
# enabled, rate limits raised well above target load)
loadtest-up:
    docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml up -d --build

# Mint fresh load-test API keys into loadtest/keys.json (safe to re-run)
loadtest-bootstrap:
    python loadtest/bootstrap.py

# Run one scenario headless against the running stack, e.g.:
#   just loadtest ThroughputUser
#   just loadtest LatencyUser
#   just loadtest BreakingPointUser
#   just loadtest EnforcementUser
#   just loadtest LatencyUser 50 15   # override users/spawn-rate
# -u/-r are ignored by ThroughputUser/BreakingPointUser (their own
# LoadTestShape governs concurrency instead).
loadtest scenario users=(if scenario == "LatencyUser" { "30" } else if scenario == "EnforcementUser" { "50" } else { "400" }) spawn_rate=(if scenario == "LatencyUser" { "10" } else if scenario == "EnforcementUser" { "10" } else { "20" }):
    locust -f loadtest/locustfile.py {{scenario}} --headless \
        -u {{users}} -r {{spawn_rate}} -t 5m --host ${TARGET_HOST:-http://localhost:8100} \
        --csv loadtest/results/{{scenario}}

# Tear down the load-test stack
loadtest-down:
    docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml down
