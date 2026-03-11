#!/usr/bin/env bash
# deploy.sh — Build and deploy PolyBot on a VPS / Railway / GCP VM
# Usage: bash scripts/deploy.sh [--env prod|staging]

set -euo pipefail

ENV=${1:-prod}
echo "==> Deploying PolyBot (env: $ENV)"

# 1. Validate required env file exists
if [[ ! -f .env ]]; then
  echo "ERROR: .env file not found. Copy .env.example and fill in your secrets."
  exit 1
fi

# 2. Pull latest image layers (speeds up rebuild)
docker compose pull --ignore-pull-failures 2>/dev/null || true

# 3. Build images
echo "==> Building Docker images …"
docker compose build --no-cache

# 4. Stop existing containers gracefully
echo "==> Stopping existing containers …"
docker compose down --remove-orphans --timeout 30 || true

# 5. Start new containers
echo "==> Starting containers …"
docker compose up -d

# 6. Wait for backend health
echo "==> Waiting for backend health check …"
RETRIES=12
for i in $(seq 1 $RETRIES); do
  if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "==> Backend healthy ✓"
    break
  fi
  echo "   Attempt $i/$RETRIES …"
  sleep 5
done

# 7. Print status
docker compose ps

echo ""
echo "==> PolyBot deployed successfully!"
echo "    Dashboard: http://localhost:80"
echo "    API:       http://localhost:8000"
echo "    API docs:  http://localhost:8000/docs"
