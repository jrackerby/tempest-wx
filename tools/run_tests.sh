#!/usr/bin/env bash
# Run the pure-layer suite. Needs no Home Assistant, no network, no token.
# rootdir must be the tests directory — see tests/pytest.ini for why.
set -euo pipefail
cd "$(dirname "$0")/../tests"
exec "${PYTHON:-python3}" -m pytest "$@"
