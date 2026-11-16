#!/bin/sh
# Container entrypoint for a hosted deploy: migrate, then serve.
#
# Migrations run here, on every start, because the free web-service tier this
# deploys to has no pre-deploy command (Render: "available for paid web
# services"). `alembic upgrade head` is a no-op when the schema is current, and
# a failure stops the container before it serves anything, so a deploy never
# runs new code against an old schema. One instance only: two starting at once
# would race on the same migration.
#
# PORT is set by the host (Render sets it; 8000 otherwise). The agent reaches
# the model through this same process's /v1 proxy, which is where the hosted
# provider's key is held, so its URL follows the port.
set -eu

PORT="${PORT:-8000}"
export AGENT_INFERENCE_BASE_URL="${AGENT_INFERENCE_BASE_URL:-http://127.0.0.1:${PORT}/v1}"

echo "start: migrating the database to head"
alembic upgrade head

echo "start: serving on port ${PORT}"
exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT}" --proxy-headers \
    --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
