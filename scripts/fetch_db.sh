#!/usr/bin/env bash
# Download the current cards.db from the GitHub Release asset into db/cards.db.
#
# The DB is no longer committed to the repo (it grew toward GitHub's 100 MB
# single-file limit); it lives as the 'cards.db' asset on the rolling 'db-latest'
# release and is published with scripts/publish_db.py. The repo is public, so no
# auth is needed to download. Called by the devcontainer on Codespace create, and
# runnable by hand: `bash scripts/fetch_db.sh` (set FORCE=1 to overwrite).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

DEST="db/cards.db"
REPO="$(git config --get remote.origin.url 2>/dev/null | sed -E 's#^.*github\.com[:/]##; s#\.git$##')"
REPO="${REPO:-Compati/trading_card_chatbot}"
URL="https://github.com/${REPO}/releases/download/db-latest/cards.db"

if [ -f "$DEST" ] && [ "${FORCE:-0}" != "1" ]; then
  echo "fetch_db: $DEST already present (set FORCE=1 to overwrite) — skipping"
  exit 0
fi

mkdir -p db
echo "fetch_db: downloading ${URL}"
if curl -fL --retry 3 -o "${DEST}.tmp" "$URL"; then
  mv "${DEST}.tmp" "$DEST"
  echo "fetch_db: wrote $DEST ($(du -h "$DEST" 2>/dev/null | cut -f1))"
else
  rm -f "${DEST}.tmp"
  echo "fetch_db: WARNING — could not download the DB asset." >&2
  echo "fetch_db: has it been published yet? run scripts/publish_db.py. The app will have no data until then." >&2
  exit 0  # non-fatal: let container setup finish
fi
