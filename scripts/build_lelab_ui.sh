#!/usr/bin/env bash
# Build LeLab's UI for a gateway sub-path (execution plan §0.19).
#
#   scripts/build_lelab_ui.sh [ROBOT] [OUT]
#
# ROBOT defaults to `lelab_so101`, OUT defaults to
# `src/plugins/lelab_so101/ui_dist` (relative to the repo root).
#
# Requires `git` and Node 22. `set -euo pipefail`.
set -euo pipefail

ROBOT="${1:-lelab_so101}"
OUT="${2:-src/plugins/lelab_so101/ui_dist}"
COMMIT="c0ec4e930c71e2b562272058166ed4c338041e7e"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="$REPO_ROOT/$OUT"
PATCH="$SCRIPT_DIR/lelab-ui-subpath.patch"

command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }
command -v node >/dev/null 2>&1 || { echo "Node 22 is required" >&2; exit 1; }

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [ "$NODE_MAJOR" != "22" ]; then
  echo "Node 22 is required (found Node $NODE_MAJOR). Install Node 22 first." >&2
  exit 1
fi
[ -f "$PATCH" ] || { echo "missing $PATCH" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Cloning leLab @ $COMMIT …"
git clone --quiet https://github.com/huggingface/leLab.git "$TMP/leLab"
git -C "$TMP/leLab" checkout --quiet "$COMMIT"
echo "Applying sub-path patch …"
git -C "$TMP/leLab" apply "$PATCH"

echo "Building frontend (base=/$ROBOT/ui/) …"
cd "$TMP/leLab/frontend"
npm ci --silent
npx vite build --base="/$ROBOT/ui/" --outDir "$OUT_DIR" --emptyOutDir

node - "$OUT_DIR" "$COMMIT" "/$ROBOT/ui/" <<'NODE'
const fs = require("fs");
const [outDir, commit, base] = process.argv.slice(2);
fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(
  `${outDir}/BUILD_INFO.json`,
  JSON.stringify({ commit, base, built_utc: new Date().toISOString() }, null, 2) + "\n"
);
NODE

echo "Built LeLab UI → $OUT_DIR"
