#!/usr/bin/env bash
# archive_session.sh — snapshot the most recent Claude Code session JSONL
# into yuxu/docs/experiences/sessions_raw/ with a dated filename.
#
# Usage: bash tools/archive_session.sh
#
# Assumes you are running Claude Code in the theme-flow-engine repo
# (the working directory that maps to ~/.claude/projects/-home-xzp-project-
# theme-flow-engine/). Adjust PROJECT_HASH below if your setup differs.

set -euo pipefail

# Anchor to the yuxu repo root regardless of where the caller ran from.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
YUXU_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST_DIR="$YUXU_ROOT/docs/experiences/sessions_raw"

# Claude Code writes session logs under ~/.claude/projects/<cwd-mangled>/.
# Accept override via env for alt setups.
PROJECT_HASH="${YUXU_CC_PROJECT_HASH:--home-xzp-project-theme-flow-engine}"
SRC_DIR="$HOME/.claude/projects/$PROJECT_HASH"

if [[ ! -d "$SRC_DIR" ]]; then
  echo "ERROR: source dir not found: $SRC_DIR" >&2
  echo "       set YUXU_CC_PROJECT_HASH env to override" >&2
  exit 1
fi

# Pick the most recently modified .jsonl at the top level of SRC_DIR.
# (Claude Code creates nested dirs per session too; the top-level .jsonl
# is the authoritative transcript.)
LATEST=$(ls -t "$SRC_DIR"/*.jsonl 2>/dev/null | head -1 || true)
if [[ -z "$LATEST" ]]; then
  echo "ERROR: no .jsonl files in $SRC_DIR" >&2
  exit 1
fi

BASENAME=$(basename "$LATEST" .jsonl)
UUID_PREFIX="${BASENAME:0:8}"
DATE=$(date -r "$LATEST" +%Y-%m-%d)
TARGET="$DEST_DIR/$DATE-$UUID_PREFIX.jsonl"

mkdir -p "$DEST_DIR"

if [[ -f "$TARGET" ]]; then
  # Don't silently overwrite — if same day + same uuid, this session is
  # probably ongoing and the content keeps growing. Just refresh the
  # copy (the filename is stable because both date and uuid are).
  echo "refreshing existing archive: $TARGET"
fi

cp "$LATEST" "$TARGET"
SIZE=$(du -h "$TARGET" | cut -f1)
LINES=$(wc -l < "$TARGET")
echo "archived: $TARGET ($SIZE, $LINES lines)"
echo "source:   $LATEST"
