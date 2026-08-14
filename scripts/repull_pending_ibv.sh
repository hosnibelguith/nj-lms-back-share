#!/usr/bin/env bash
# Re-queue GetAccountsDetail for stuck / pending IBV (same LoginId, no new Connect).
# Default is dry-run. Pass --apply to enqueue.
#
# Local:
#   ./scripts/repull_pending_ibv.sh
#   ./scripts/repull_pending_ibv.sh --since 2026-08-14 --apply
#
# Heroku — quote the whole command so flags are not eaten by the CLI:
#   heroku run "python manage.py repull_pending_ibv --since 2026-08-14" -a nj-lms-back
#   heroku run "python manage.py repull_pending_ibv --since 2026-08-14 --apply" -a nj-lms-back
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PYTHON="${ROOT}/.venv/bin/python"
else
  PYTHON="${PYTHON:-python}"
fi

exec "$PYTHON" manage.py repull_pending_ibv "$@"
