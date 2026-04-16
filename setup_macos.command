#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# macOS GUI-launched terminals may have a limited PATH; add common locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

bash "$ROOT/setup_macos.sh"

echo ""
echo "Press Enter to close..."
read -r _

