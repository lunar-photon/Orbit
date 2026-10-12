#!/usr/bin/env bash
# Remove Orbit and restore password-only login. Keeps ~/.config/orbit/config.json
# (phone pairing) unless --purge is given. Supports --dry-run.
set -euo pipefail
exec bash "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/install.sh" --uninstall "$@"
