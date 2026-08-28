#!/bin/sh
set -eu

schedule_file="$(mktemp /tmp/career-monitor-crontab.XXXXXX)"
career-monitor render-schedule >"$schedule_file"
exec /usr/local/bin/supercronic -passthrough-logs "$schedule_file"
