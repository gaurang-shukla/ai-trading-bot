#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Signal is not installed yet. Run the setup commands in README.md first."
  read -r -p "Press Return to close."
  exit 1
fi
source .venv/bin/activate
# Always run the checkout's current source.  A console script from a previous
# non-editable install can otherwise keep serving stale provider code after a
# pull, which is particularly dangerous for market-data normalization fixes.
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -m tradebot.app
