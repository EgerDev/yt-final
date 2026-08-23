#!/bin/bash
# macOS double-click launcher — equivalent to Start yt_final.bat
cd "$(dirname "$0")" || exit 1

echo ""
echo "  Starting yt_final..."
echo "  Browser will open. Your login password is shown below and saved to webui.txt."
echo "  Leave THIS window open while you use the app."
echo ""

python_is_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
}

# An operator-created local project venv may be used. The app-data managed
# target is install state and is never probed or executed by this launcher.
if [ -x "./.venv/bin/python" ] && python_is_supported "./.venv/bin/python"; then
  exec "./.venv/bin/python" yt_final.py serve
fi

if command -v python3 >/dev/null 2>&1 && python_is_supported python3; then
  python3 yt_final.py serve
elif command -v python >/dev/null 2>&1 && python_is_supported python; then
  python yt_final.py serve
else
  echo "  Python 3.11 or newer was not found. Install Python 3.11+ and try again."
  echo ""
  read -r -p "  Press Enter to close…"
  exit 1
fi

echo ""
echo "  yt_final stopped."
read -r -p "  Press Enter to close…"
