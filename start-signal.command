#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Signal is not installed yet. Run the setup commands in README.md first."
  read -r -p "Press Return to close."
  exit 1
fi
source .venv/bin/activate
exec signal-app
