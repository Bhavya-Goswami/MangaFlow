#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="work/.venv"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
OPEN_URL="${OPEN_URL:-1}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3 was not found. Install Python 3, then run this again."
  exit 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating local Python environment..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo "Installing/updating Python packages..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install -r requirements.txt

if [ -d "/opt/homebrew/opt/openjdk/bin" ]; then
  export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk}"
  export PATH="/opt/homebrew/opt/openjdk/bin:$PATH"
elif [ -d "/usr/local/opt/openjdk/bin" ]; then
  export JAVA_HOME="${JAVA_HOME:-/usr/local/opt/openjdk}"
  export PATH="/usr/local/opt/openjdk/bin:$PATH"
fi

if ! java -version >/dev/null 2>&1; then
  echo
  echo "Java is required for Nyora's local helper."
  echo "On macOS with Homebrew, run: brew install openjdk"
  echo "Then run ./run.sh again."
  exit 1
fi

URL="http://$HOST:$PORT/"
echo
echo "MangaFlow is starting at $URL"
echo "Press Ctrl+C here to stop it."

if [ "$OPEN_URL" = "1" ]; then
  (sleep 1.5 && open "$URL" >/dev/null 2>&1 || true) &
fi

exec "$VENV_DIR/bin/python" server.py
