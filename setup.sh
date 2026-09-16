#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
cd "$ROOT_DIR"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<'EOF'
Usage: ./setup.sh

Check Python 3.10+, create .venv, and install requirements.

Examples:
  ./setup.sh
  ./setup.sh --help
EOF
    exit 0
fi
if [[ $# -gt 0 ]]; then
    echo "Error: unsupported argument: $1" >&2
    echo "Run ./setup.sh --help for usage." >&2
    exit 2
fi

PYTHON_CMD=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'
    then
        PYTHON_CMD="$(command -v "$candidate")"
        break
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    echo "Error: Python 3.10 or later is required but was not found." >&2
    exit 1
fi

"$PYTHON_CMD" - <<'PY'
import sys

if sys.version_info < (3, 10):
    raise SystemExit(
        f"Error: Python 3.10 or later is required; found {sys.version.split()[0]}"
    )
print(f"Python {sys.version.split()[0]}: ready")
PY

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
"$VENV_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || {
    echo "Error: the existing .venv uses Python older than 3.10. Remove it and rerun setup.sh." >&2
    exit 1
}
echo "Installing dependencies..."
"$VENV_PYTHON" -m pip install --quiet --upgrade pip
"$VENV_PYTHON" -m pip install --quiet -r "$ROOT_DIR/requirements.txt"

echo
echo "Setup complete."
echo "Activate with: source \"$VENV_DIR/bin/activate\""
