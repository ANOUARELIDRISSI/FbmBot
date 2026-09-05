#!/bin/sh
# A freshly created container's network sometimes isn't fully up for its
# first couple of seconds (seen for real: Bot.get_me() timed out on a
# fresh start, then succeeded instantly moments later on the same
# container) — cheap insurance against that race before doing anything
# network-bound.
sleep 3

# cron doesn't inherit the container's environment (TELEGRAM_BOT_TOKEN,
# MISTRAL_API_KEY, ...) — snapshot it once to a file that run_pipeline.sh
# sources on every cron tick. Excludes a couple of noisy/irrelevant vars.
printenv | grep -Ev '^(HOSTNAME|PWD|OLDPWD|_)=' \
    | sed -E 's/^([^=]+)=(.*)$/export \1="\2"/' > /app/env_for_cron.sh

# Started before the first run (not after) so `docker logs -f` shows the
# initial run's progress live, not just once it finishes.
mkdir -p /app/data
touch /app/data/pipeline.log /app/data/feedback_listener.log
tail -F /app/data/pipeline.log /app/data/feedback_listener.log &

# `becarscout listen` (stage 7's Telegram thumbs up/down capture) has to run
# continuously, not on a cron tick like the pipeline — otherwise a button
# press just goes nowhere, since nothing is polling Telegram for it. Runs
# in the background here so cron (below) can still be PID 1 in the
# foreground; if this process dies it won't auto-restart until the whole
# container does (no supervisor set up — acceptable at this project's
# scale, but a real limitation, see Project.md stage 7 notes).
echo "[entrypoint] starting feedback listener (captures Telegram thumbs up/down presses)..."
uv run becarscout listen >> /app/data/feedback_listener.log 2>&1 &

echo "[entrypoint] running pipeline once at startup..."
/app/docker/run_pipeline.sh || echo "[entrypoint] initial run failed (see data/pipeline.log) — will retry on the next hourly tick"

echo "[entrypoint] startup run done, starting cron (hourly schedule)..."
exec cron -f
