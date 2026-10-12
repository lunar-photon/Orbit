#!/usr/bin/env bash
# Orbit: install the computer side. All options are passed to linux/install.sh
# (see: ./setup.sh --help).
set -euo pipefail
exec bash "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/linux/install.sh" "$@"
