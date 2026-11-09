#!/bin/sh
# Start AETHER CE as a plain Python process on 127.0.0.1 (no venv, no pip). Needs Python 3.11+.
cd "$(dirname "$0")" || exit 1
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PYTHONPATH="$(pwd)${PYTHONPATH:+:$PYTHONPATH}"
    export PYTHONPATH
    exec "$cand" -m aether.studio serve --open "$@"
  fi
done
echo "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/" >&2
exit 1
