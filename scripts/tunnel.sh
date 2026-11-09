#!/usr/bin/env bash
# Thin wrapper around `trainmon tunnel` for people who prefer shell scripts.
#
# Usage:
#   ./scripts/tunnel.sh [server-name] [--local-port 8787] [--background]
#
# Before first use:
#   python3 -m trainmon config init
#   # edit trainmon.ini or ~/.trainmon/config.ini
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec env PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m trainmon tunnel "$@"
