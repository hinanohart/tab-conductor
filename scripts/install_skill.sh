#!/usr/bin/env bash
# Install tab-conductor skill to ~/.claude/skills/tab-conductor/
# Usage: bash scripts/install_skill.sh
#
# WARNING: This script overwrites ~/.claude/skills/tab-conductor/ completely.
# If you have local customizations there, back them up first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/../skill"
DST="${HOME}/.claude/skills/tab-conductor"

command -v rsync >/dev/null 2>&1 || { echo "rsync required" >&2; exit 1; }
[ -d "$SRC" ] || { echo "skill source not found: $SRC" >&2; exit 1; }

mkdir -p "$DST"
rsync -a --delete \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$SRC/" "$DST/"

echo "Installed tab-conductor skill to: $DST"
echo "Verify: ls -la $DST/SKILL.md"
