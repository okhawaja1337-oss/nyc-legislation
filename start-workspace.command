#!/bin/bash
# Start the D49 workspace on macOS or Linux.
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  python3 START_WORKSPACE.py "$@"
else
  echo "Python 3 was not found. Install it from python.org and try again."
  read -r -p "Press Enter to close."
fi
