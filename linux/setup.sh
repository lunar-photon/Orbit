#!/usr/bin/env bash
# Compatibility wrapper: the old PC Authenticator setup flags, mapped onto
# install.sh. New usage: linux/install.sh --help
set -euo pipefail
here="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
args=()
for a in "$@"; do
    case "$a" in
        --install|--passwordless|-1) args+=(--mode passwordless) ;;
        --2fa|-2) args+=(--mode 2fa) ;;
        --revert|--remove|-r) args+=(--revert-pam) ;;
        --enroll-usb|--usb) exec python3 "$here/enroll-usb.py" ;;
        --status|-s) exec python3 "$here/orbit" status ;;
        *) args+=("$a") ;;
    esac
done
exec bash "$here/install.sh" "${args[@]}"
