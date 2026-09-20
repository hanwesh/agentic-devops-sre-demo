#!/usr/bin/env bash
# All safety checks, request bounds, and evidence handling live in the Python CLI.
set -euo pipefail
exec "${PYTHON:-python3}" "$(dirname "${BASH_SOURCE[0]}")/traffic.py" "$@"
