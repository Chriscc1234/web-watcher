#!/usr/bin/env bash
# Web Watcher Installer — Linux / macOS
# Usage: bash install.sh [options]
# Options are passed through to install.py (--skip-models, --tier, etc.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Check Python
# ---------------------------------------------------------------------------

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major="${version%%.*}"
        minor="${version#*.}"
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ] 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo ""
    echo "  ERROR: Python 3.10+ not found."
    echo ""
    echo "  Install Python via your package manager, e.g.:"
    echo "    Ubuntu/Debian : sudo apt install python3.11"
    echo "    Fedora        : sudo dnf install python3.11"
    echo "    macOS         : brew install python@3.11"
    echo "    or            : https://www.python.org/downloads/"
    echo ""
    exit 1
fi

echo "  Using: $PYTHON ($($PYTHON --version))"

# ---------------------------------------------------------------------------
# Run the Python installer
# ---------------------------------------------------------------------------

exec "$PYTHON" "$SCRIPT_DIR/install.py" "$@"
