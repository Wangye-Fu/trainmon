#!/usr/bin/env bash
# Thin wrapper around `trainmon deploy` for people who prefer shell scripts.
#
# Usage:
#   ./scripts/deploy.sh [server-name]
#
# Before first use:
#   python3 -m trainmon config init
#   # edit trainmon.ini or ~/.trainmon/config.ini
#
# You can also run the CLI directly:
#   python3 -m trainmon deploy <server-name>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec env PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m trainmon deploy "$@"
