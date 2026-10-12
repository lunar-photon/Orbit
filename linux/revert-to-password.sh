#!/usr/bin/env bash
# Restore password-only login and lock screen; Orbit itself stays installed.
set -euo pipefail
exec bash "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/install.sh" --revert-pam "$@"
