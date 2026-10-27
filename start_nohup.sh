#!/usr/bin/env bash
# Kept for old callers: always go through the supervise loop so there is only one runner.
cd "$(dirname "$0")"; exec ./supervise.sh start
