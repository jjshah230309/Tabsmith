#!/bin/bash
# Tabsmith launcher — runs the CLI from its own virtualenv, from any directory.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env PYTHONPATH="$HERE" "$HERE/.venv/bin/python" -m tabsmith.cli "$@"
