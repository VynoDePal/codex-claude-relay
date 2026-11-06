#!/usr/bin/env bash
# Lance la suite de tests du relais (unittest, sans dépendance réseau).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../relay"
python3 -m unittest discover -p 'test_*.py' "$@"
cd ../gpu-agents
exec python3 -m unittest discover -p 'test_*.py' "$@"

