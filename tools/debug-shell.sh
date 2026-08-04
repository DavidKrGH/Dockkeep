#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${DK_DEBUG_PYTHON:-$PROJECT_ROOT/.venv/bin/python}"

cd "$PROJECT_ROOT"

export PATH="$PROJECT_ROOT/.venv/bin:$PATH"
export DK_DEBUG_PYTHON="$PYTHON_BIN"
export DK_DEBUG_HOST="${DK_DEBUG_HOST:-127.0.0.1}"
export DK_DEBUG_PORT="${DK_DEBUG_PORT:-5678}"

dk-debug() {
  "$DK_DEBUG_PYTHON" -Xfrozen_modules=off \
    -m debugpy --listen "$DK_DEBUG_HOST:$DK_DEBUG_PORT" --wait-for-client \
    -m src.main "$@"
}

dk-runtime-debug() {
  "$DK_DEBUG_PYTHON" -Xfrozen_modules=off \
    -m debugpy --listen "$DK_DEBUG_HOST:$DK_DEBUG_PORT" --wait-for-client \
    -m src.runtime "$@"
}

export -f dk-debug
export -f dk-runtime-debug

echo "Debug shell ready."
echo "  DK_MODE=${DK_MODE:-unset}"
echo "  dk <command>                runs the CLI normally"
echo "  dk-debug <command>          waits for VS Code attach on ${DK_DEBUG_HOST}:${DK_DEBUG_PORT}"
echo "  dk-runtime-debug <command>  debugs runtime processes"
echo
echo "Example:"
echo "  dk-runtime-debug scheduler"
echo
echo "In VS Code, attach with: Dockkeep: an debugpy anhaengen"

exec bash -i
