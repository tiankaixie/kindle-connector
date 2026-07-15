#!/usr/bin/env bash
# Convenience launcher for papershell.
# Override anything via env, e.g.:  KINDLE_PORT=9000 KINDLE_CMD=codex ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

export KINDLE_PORT="${KINDLE_PORT:-8090}"
export KINDLE_COLS="${KINDLE_COLS:-58}"   # terminal width  (fixed, fits Kindle)
export KINDLE_ROWS="${KINDLE_ROWS:-32}"   # terminal height (fixed)
export KINDLE_CMD="${KINDLE_CMD:-claude}" # claude | codex | any command
# export KINDLE_TOKEN="changeme"          # uncomment to require ?t=changeme
# export KINDLE_WORKDIR="$HOME/codebase"  # where the agent starts

exec python3 server.py
