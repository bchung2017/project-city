#!/usr/bin/env bash
# SessionStart hook — prepares a fresh Claude Code session (esp. web containers,
# which clone the repo clean) so edits can be verified immediately.
#
# It (1) installs Python deps and (2) does a DB-free syntax check of the app.
# It deliberately does NOT boot the app or import it: app.py reads DATABASE_URL
# at import time and crashes without it, and sessions may not have that secret.
#
# Non-fatal by design: a failed install shouldn't block the session. We report
# and exit 0 so Claude still starts.
set -u

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root" || exit 0

echo "[session-start] installing dependencies…"
if command -v pip >/dev/null 2>&1; then
  pip install -q -r requirements.txt 2>&1 | tail -n 3 || echo "[session-start] pip install had issues (continuing)"
else
  echo "[session-start] pip not found — skipping dependency install"
fi

echo "[session-start] syntax-checking main/app.py…"
if python -c "import ast,sys; ast.parse(open('main/app.py').read()); print('[session-start] app.py parses OK')"; then
  :
else
  echo "[session-start] WARNING: main/app.py failed to parse"
fi

if [ -z "${DATABASE_URL:-}" ]; then
  echo "[session-start] note: DATABASE_URL is unset — the app cannot boot or hit /healthz in this session."
fi

exit 0
