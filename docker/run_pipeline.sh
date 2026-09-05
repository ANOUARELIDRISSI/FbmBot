#!/bin/sh
# Invoked by cron (no inherited shell environment) and, once, directly by
# entrypoint.sh at container startup — sources the env snapshot entrypoint.sh
# wrote out, since cron itself never sees the container's environment.
cd /app || exit 1
[ -f /app/env_for_cron.sh ] && . /app/env_for_cron.sh

mkdir -p /app/data
uv run becarscout run >> /app/data/pipeline.log 2>&1
