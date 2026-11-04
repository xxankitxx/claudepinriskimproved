#!/usr/bin/env bash
# =============================================================================
# VAJRA operator orchestrator (POSIX).  Mirrors the MYTHOS operator-run model.
# Separation of powers (spec sec.11): this script does not read council/verdict files.
#
# Usage:
#   ./orchestrator.sh                      # live (needs BREEZE_* env vars)
#   ./orchestrator.sh synthetic            # dev smoke (NON-BINDING data)
#   ./orchestrator.sh replay 2026-06-16
#   ./orchestrator.sh verify               # completion gate only
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-live}"
DATE_ARG="${2:-}"

if [[ "$MODE" == "verify" ]]; then
    exec python verify_vajra.py
fi

echo "VAJRA 2.0 - pin-risk / gamma-explosion engine (mode=$MODE)"

# Gate the build before any session.
python verify_vajra.py || { echo "Completion gate FAILED - refusing to run."; exit 1; }

if [[ "$MODE" == "live" ]]; then
    if [[ ! -f vajra/credentials.py ]]; then
        echo "Missing vajra/credentials.py - fill API_KEY/API_SECRET/SESSION_KEY before live mode."; exit 1
    fi
    echo "Reminder: SESSION_KEY in vajra/credentials.py expires daily - refresh before 09:15 IST."
fi

ARGS=(-m vajra.app --mode "$MODE")
[[ -n "$DATE_ARG" ]] && ARGS+=(--date "$DATE_ARG")
exec python "${ARGS[@]}"
