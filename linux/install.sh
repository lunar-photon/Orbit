#!/usr/bin/env bash
# "~/..." strings are display labels; *_BLOCK texts are written out literally.
# shellcheck disable=SC2088,SC2016
# =============================================================================
# Orbit installer (computer side)
#
# Run as your normal user. sudo is used only for: installing packages, the PAM
# helper in /usr/local/bin, and files under /etc.
#
#   install.sh [--dry-run] [--no-term-history] [--no-pam] [--mode 2fa|passwordless]
#   install.sh --uninstall [--purge] [--dry-run]
#   install.sh --revert-pam [--dry-run]     # password-only login, keep Orbit
# =============================================================================
set -euo pipefail

SELF="$(readlink -f -- "${BASH_SOURCE[0]}")"
SRC="$(dirname -- "$SELF")"
REPO="$(dirname -- "$SRC")"

# ---- names (keep in sync with orbitd.py / orbit-tray.py) --------------------
HELPER=/usr/local/bin/orbit-auth-check
OLD_HELPER=/usr/local/bin/lockscreen-auth-check
ETC_DIR=/etc/orbit
OLD_ETC_DIR=/etc/pc-auth
PAM_BACKUP_DIR=/etc/orbit/pam-backup
PAM_NAMES=(kde plasmalogin sddm)
UDEV_RULE=/etc/udev/rules.d/99-orbit-uinput.rules
OLD_UDEV_RULE=/etc/udev/rules.d/99-pc-connect-uinput.rules
MODLOAD=/etc/modules-load.d/orbit-uinput.conf
CAM_MODPROBE=/etc/modprobe.d/orbit-cam.conf
CAM_MODLOAD=/etc/modules-load.d/orbit-cam.conf
DM_DROPIN_DIR=/etc/systemd/system/plasmalogin.service.d
DM_DROPIN="$DM_DROPIN_DIR/orbit.conf"
MARK_BEGIN='# >>> orbit >>>'
MARK_END='# <<< orbit <<<'

# ---- options ----------------------------------------------------------------
DRY=0 NO_TMUX=0 NO_PAM=0 PURGE=0 MODE="" ACTION=install
ROOT_STEP="" TARGET_USER=""

usage() {
    cat <<'EOF'
Usage: install.sh [options]

  --dry-run                 Show every action; change nothing
  --no-term-history         Don't record Konsole tabs (phone terminal scrollback)
  --no-pam                  Skip login / lock screen integration (no PAM changes)
  --mode 2fa|passwordless   Unlock mode. Default: keep the current one; if none
                            is installed, ask (default 2fa)
  --uninstall               Restore password-only login and remove Orbit
                            (keeps ~/.config/orbit/config.json)
  --purge                   With --uninstall: also delete config and pairings
  --revert-pam              Only restore password-only login
  -h, --help                Show this help
EOF
}

while (($#)); do
    case "$1" in
        --dry-run|-n) DRY=1 ;;
        --no-term-history|--no-tmux) NO_TMUX=1 ;;
        --no-pam) NO_PAM=1 ;;
        --mode) shift; MODE="${1:-}" ;;
        --mode=*) MODE="${1#--mode=}" ;;
        --uninstall) ACTION=uninstall ;;
        --purge) PURGE=1 ;;
        --revert-pam) ACTION=revert-pam ;;
        --root-step) shift; ROOT_STEP="${1:-}" ;;   # internal (runs under sudo)
        --user) shift; TARGET_USER="${1:-}" ;;      # internal
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
case "$MODE" in ""|2fa|passwordless) ;; *) echo "--mode must be 2fa or passwordless" >&2; exit 2 ;; esac

# ---- output -----------------------------------------------------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_B=$'\e[1m' C_DIM=$'\e[2m' C_RED=$'\e[31m' C_GRN=$'\e[32m' C_YEL=$'\e[33m' C_BLU=$'\e[34m' C_OFF=$'\e[0m'
else
    C_B="" C_DIM="" C_RED="" C_GRN="" C_YEL="" C_BLU="" C_OFF=""
fi
step() { printf '\n%s==>%s %s%s%s\n' "$C_BLU" "$C_OFF" "$C_B" "$*" "$C_OFF"; }
ok()   { printf '  %sok%s    %s\n' "$C_GRN" "$C_OFF" "$*"; }
info() { printf '  %s..%s    %s\n' "$C_DIM" "$C_OFF" "$*"; }
skip() { printf '  %sskip%s  %s\n' "$C_DIM" "$C_OFF" "$*"; }
warn() { printf '  %swarn%s  %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

SUDO_PFX=""   # shown in dry-run output for root actions
qcmd() { local a out=""; for a in "$@"; do out+="$(printf '%q' "$a") "; done; printf '%s' "${out% }"; }
run() {
    if ((DRY)); then printf '  %s[dry-run]%s %s%s\n' "$C_YEL" "$C_OFF" "$SUDO_PFX" "$(qcmd "$@")"; return 0; fi
    "$@"
}
# run, but never fail the script (best-effort housekeeping)
try() {
    if ((DRY)); then run "$@"; else "$@" >/dev/null 2>&1 || true; fi
}

# write_file DEST MODE CONTENT  -- atomic, skips if unchanged
write_file() {
    local dest="$1" mode="$2" content="$3" tmp
    if [[ -f "$dest" && ! -L "$dest" ]] && [[ "$(<"$dest")" == "$content" ]]; then
        if [[ "$(stat -c %a -- "$dest")" != "$mode" ]]; then run chmod "$mode" -- "$dest"; fi
        skip "$dest unchanged"
        return 0
    fi
    if ((DRY)); then
        printf '  %s[dry-run]%s %swrite %s (mode %s, %s lines)\n' "$C_YEL" "$C_OFF" "$SUDO_PFX" "$dest" "$mode" "$(printf '%s\n' "$content" | wc -l)"
        return 0
    fi
    tmp="$(mktemp -- "$(dirname -- "$dest")/.orbit-tmp.XXXXXX")"
    printf '%s\n' "$content" >"$tmp"
    chmod "$mode" -- "$tmp"
    mv -f -- "$tmp" "$dest"
    ok "wrote $dest"
}

# link NAME TARGET  -- symlink, skips if already correct
link() {
    local path="$1" target="$2"
    if [[ -L "$path" && "$(readlink -- "$path")" == "$target" ]]; then skip "$path -> $target"; return 0; fi
    run ln -sfn -- "$target" "$path"
    ((DRY)) || ok "$path -> $target"
}

# mkd MODE DIR...  -- create missing dirs only (never chmod an existing one)
mkd() {
    local mode="$1" d; shift
    for d in "$@"; do [[ -d "$d" ]] || run install -d -m "$mode" -- "$d"; done
}

# remove PATH (file, symlink or dir) if present
remove() {
    local p
    for p in "$@"; do
        if [[ -e "$p" || -L "$p" ]]; then run rm -rf -- "$p"; ((DRY)) || ok "removed $p"; fi
    done
}

# =============================================================================
# PAM handling (shared by root steps and dry-run previews)
# =============================================================================
# helper lines: pam_exec.so calling either the old or the new helper path
is_helper_re='pam_exec\.so.*(/lockscreen-auth-check|/orbit-auth-check)'

pam_mode_of() {   # content -> passwordless | 2fa | none
    local lines
    lines="$(printf '%s\n' "$1" | grep -E -- "$is_helper_re" || true)"
    if [[ -z "$lines" ]]; then echo none
    elif grep -q -- '--passwordless' <<<"$lines"; then echo passwordless
    else echo 2fa; fi
}

pam_strip() {     # content -> content without helper lines
    printf '%s\n' "$1" | grep -Ev -- "$is_helper_re" || true
}

# pam_render CONTENT MODE -> new content on stdout (non-zero if no anchor)
pam_render() {
    local content="$1" mode="$2"
    if [[ "$(pam_mode_of "$content")" == "$mode" ]]; then
        # Same mode: exact-match substitution of the old helper path only.
        printf '%s\n' "$content" | OLD="$OLD_HELPER" NEW="$HELPER" awk '
            index($0, "pam_exec.so") && (i = index($0, ENVIRON["OLD"])) {
                $0 = substr($0, 1, i - 1) ENVIRON["NEW"] substr($0, i + length(ENVIRON["OLD"]))
            }
            { print }'
        return 0
    fi
    # Mode change / fresh install: drop any helper line, insert one new line.
    pam_strip "$content" | MODE="$mode" \
        L2="auth        required    pam_exec.so quiet $HELPER" \
        LP="auth        [success=done default=ignore] pam_exec.so expose_authtok quiet $HELPER --passwordless" \
        awk '
            !done && ENVIRON["MODE"] == "passwordless" && /^-?auth[ \t]/ { print ENVIRON["LP"]; done = 1 }
            { print }
            !done && ENVIRON["MODE"] == "2fa" && /^auth[ \t]+(include|substack)[ \t]/ { print ENVIRON["L2"]; done = 1 }
            END { if (!done) exit 3 }'
}

# pam_verify OLD NEW MODE -> 0 if NEW is OLD plus exactly one correct helper line
pam_verify() {
    local old="$1" new="$2" mode="$3"
    [[ "$(printf '%s\n' "$new" | grep -E -- "$is_helper_re" | grep -cF -- " $HELPER")" == 1 ]] || return 1
    ! printf '%s\n' "$new" | grep -qF -- "$OLD_HELPER" || return 1
    [[ "$(pam_mode_of "$new")" == "$mode" ]] || return 1
    [[ "$(pam_strip "$old")" == "$(pam_strip "$new")" ]] || return 1
    printf '%s\n' "$new" | grep -qE '^-?auth[ \t]' || return 1
}

detect_pam_mode() {
    local name f m
    for name in "${PAM_NAMES[@]}"; do
        f="/etc/pam.d/$name"
        [[ -r "$f" ]] || continue
        m="$(pam_mode_of "$(<"$f")")"
        if [[ "$m" != none ]]; then echo "$m"; return; fi
    done
    echo none
}

show_diff() {     # label old new
    if ((DRY)); then
        diff -u --label "$1 (now)" --label "$1 (after)" <(printf '%s\n' "$2") <(printf '%s\n' "$3") | sed 's/^/      /' || true
    fi
}

# Apply planned PAM changes with backup + verify + rollback.
# Globals: PLAN_NAMES PLAN_ACTIONS(write|delete) PLAN_CONTENT
pam_apply_plan() {
    local ts i name dest act backup failed=0
    local -a done_names=() done_backups=()
    ts="$(date +%Y%m%d-%H%M%S)"
    run install -d -m 700 -o root -g root -- "$PAM_BACKUP_DIR"
    for i in "${!PLAN_NAMES[@]}"; do
        name="${PLAN_NAMES[$i]}" act="${PLAN_ACTIONS[$i]}" dest="/etc/pam.d/${PLAN_NAMES[$i]}"
        backup=""
        if [[ -f "$dest" ]]; then
            backup="$PAM_BACKUP_DIR/$name.$ts"
            run cp -a -- "$dest" "$backup" || { failed=1; break; }
        fi
        done_names+=("$name"); done_backups+=("$backup")
        if [[ "$act" == delete ]]; then
            run rm -f -- "$dest" || { failed=1; break; }
            if ((!DRY)) && [[ -e "$dest" || ! -f "/usr/lib/pam.d/$name" ]]; then failed=1; break; fi
            ((DRY)) || ok "$dest removed (vendor /usr/lib/pam.d/$name applies)"
        else
            write_file "$dest" 644 "${PLAN_CONTENT[$i]}" || { failed=1; break; }
            if ((!DRY)); then
                chown root:root -- "$dest" || { failed=1; break; }
                # re-read what is on disk and check it is exactly what we meant
                if [[ "$(<"$dest")" != "${PLAN_CONTENT[$i]}" ]]; then failed=1; break; fi
            fi
        fi
    done
    if ((failed)); then
        warn "PAM update failed; restoring the previous files"
        for i in "${!done_names[@]}"; do
            dest="/etc/pam.d/${done_names[$i]}"
            if [[ -n "${done_backups[$i]}" ]]; then cp -a -- "${done_backups[$i]}" "$dest" && warn "restored $dest"
            else rm -f -- "$dest" && warn "removed $dest (did not exist before)"; fi
        done
        return 1
    fi
    return 0
}

# Plan "install helper line in MODE" for each relevant PAM service.
pam_plan_install() {
    local mode="$1" name etc vendor base new
    PLAN_NAMES=() PLAN_ACTIONS=() PLAN_CONTENT=()
    for name in "${PAM_NAMES[@]}"; do
        etc="/etc/pam.d/$name" vendor="/usr/lib/pam.d/$name"
        if [[ -f "$etc" ]]; then base="$(<"$etc")"
        elif [[ -f "$vendor" ]]; then base="$(<"$vendor")"; info "$etc will be created from $vendor"
        else skip "$name: no PAM service on this system"; continue; fi
        if ! new="$(pam_render "$base" "$mode")"; then
            warn "$etc: no 'auth include' line to anchor on; left unchanged"
            continue
        fi
        if [[ -f "$etc" && "$new" == "$base" ]]; then skip "$etc already uses $HELPER ($mode)"; continue; fi
        if ! pam_verify "$base" "$new" "$mode"; then
            warn "$etc: generated stack failed verification; left unchanged"
            continue
        fi
        show_diff "$etc" "$base" "$new"
        PLAN_NAMES+=("$name") PLAN_ACTIONS+=(write) PLAN_CONTENT+=("$new")
    done
}

norm_pam() {      # drop comments/blank lines, squeeze whitespace
    printf '%s\n' "$1" | sed -e 's/#.*//' -e 's/[[:space:]]\+/ /g' -e 's/^ //' -e 's/ $//' | grep -v '^$' || true
}

# Plan "remove helper line" (password-only) for each PAM service.
pam_plan_revert() {
    local name etc vendor base stripped
    PLAN_NAMES=() PLAN_ACTIONS=() PLAN_CONTENT=()
    for name in "${PAM_NAMES[@]}"; do
        etc="/etc/pam.d/$name" vendor="/usr/lib/pam.d/$name"
        [[ -f "$etc" ]] || continue
        base="$(<"$etc")"
        if [[ "$(pam_mode_of "$base")" == none ]]; then skip "$etc has no Orbit line"; continue; fi
        stripped="$(pam_strip "$base")"
        if ! grep -qE '^-?auth[ \t]' <<<"$stripped"; then
            warn "$etc: would have no auth lines left; left unchanged"; continue
        fi
        if [[ -f "$vendor" && "$(norm_pam "$stripped")" == "$(norm_pam "$(<"$vendor")")" ]]; then
            info "$etc matches $vendor apart from Orbit; removing the override"
            PLAN_NAMES+=("$name") PLAN_ACTIONS+=(delete) PLAN_CONTENT+=("")
        else
            show_diff "$etc" "$base" "$stripped"
            PLAN_NAMES+=("$name") PLAN_ACTIONS+=(write) PLAN_CONTENT+=("$stripped")
        fi
    done
}

pam_refs_left() { grep -lE -- "$is_helper_re" /etc/pam.d/* 2>/dev/null || true; }

# =============================================================================
# Root steps (run via sudo, or previewed in-process during --dry-run)
# =============================================================================
root_common() {   # uinput access for the virtual trackpad, firewall, linger
    step "System: input device, firewall, lingering"
    write_file "$MODLOAD" 644 "uinput"
    write_file "$UDEV_RULE" 644 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"'
    remove "$OLD_UDEV_RULE"
    try modprobe uinput
    try udevadm control --reload-rules
    try udevadm trigger --sysname-match=uinput

    # Only local networks reach Orbit directly; the internet comes in through the tunnel
    local nets=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16)
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'; then
        # older versions opened the ports to everyone
        try ufw delete allow 1760/tcp
        try ufw delete allow 1760/udp
        try ufw delete allow 1762/udp
        for net in "${nets[@]}"; do
            try ufw allow from "$net" to any port 1760 proto tcp comment Orbit
            try ufw allow from "$net" to any port 1760 proto udp comment Orbit
            try ufw allow from "$net" to any port 1762 proto udp comment Orbit
        done
        ok "ufw: 1760/tcp, 1760/udp, 1762/udp allowed from local networks"
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        try firewall-cmd --permanent --remove-port=1760/tcp --remove-port=1760/udp --remove-port=1762/udp
        for net in "${nets[@]}"; do
            for port in 1760/tcp 1760/udp 1762/udp; do
                try firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=$net port port=${port%/*} protocol=${port#*/} accept"
            done
        done
        try firewall-cmd --reload
        ok "firewalld: 1760/tcp, 1760/udp, 1762/udp allowed from local networks"
    else
        skip "no active firewall (ufw / firewalld)"
    fi

    # Orbit Cam: a virtual camera (v4l2loopback) that the phone's camera streams into, named
    # so in video-call apps. exclusive_caps: apps see it as a camera (Orbit keeps it fed).
    if modinfo v4l2loopback >/dev/null 2>&1; then
        write_file "$CAM_MODPROBE" 644 'options v4l2loopback devices=1 exclusive_caps=1 card_label="Orbit Cam"'
        write_file "$CAM_MODLOAD" 644 "v4l2loopback"
        # takes effect now if no app has the camera open, otherwise at the next boot
        if try modprobe -r v4l2loopback; then try modprobe v4l2loopback; fi
    else
        skip "v4l2loopback not installed: Orbit Cam needs it (v4l2loopback-dkms)"
    fi

    # The daemon must run before anyone logs in (boot login screen approval).
    try loginctl enable-linger "$TARGET_USER"
    ((DRY)) || ok "lingering enabled for $TARGET_USER"
}

root_auth_install() {
    local mode="$1" rc tmp ts
    step "Login integration: PAM helper"
    # 1. Sanity-check the source before it goes anywhere near PAM.
    python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$SRC/orbit-auth-check" \
        || die "linux/orbit-auth-check does not parse; PAM left untouched"

    # 2. Install the new helper atomically.
    if [[ -f "$HELPER" ]] && cmp -s -- "$SRC/orbit-auth-check" "$HELPER"; then
        skip "$HELPER up to date"
    elif ((DRY)); then
        run install -o root -g root -m 755 -- "$SRC/orbit-auth-check" "$HELPER"
    else
        tmp="$(mktemp /usr/local/bin/.orbit-auth-check.XXXXXX)"
        install -o root -g root -m 755 -- "$SRC/orbit-auth-check" "$tmp"
        mv -f -- "$tmp" "$HELPER"
        ok "installed $HELPER"
    fi

    # 3. Smoke test: root is never handled -> 2fa exits 0, passwordless exits 1.
    if ((!DRY)); then
        rc=0; env -i PATH=/usr/bin:/bin PAM_USER=root "$HELPER" </dev/null || rc=$?
        [[ $rc == 0 ]] || die "$HELPER smoke test failed (2fa exit $rc); PAM left untouched"
        rc=0; env -i PATH=/usr/bin:/bin PAM_USER=root "$HELPER" --passwordless </dev/null || rc=$?
        [[ $rc == 1 ]] || die "$HELPER smoke test failed (passwordless exit $rc); PAM left untouched"
        ok "$HELPER runs (smoke test passed)"
    else
        info "would smoke-test $HELPER (PAM_USER=root: 2fa -> 0, --passwordless -> 1)"
    fi

    # 4. Old path keeps working (PAM files still point there until step 6).
    if [[ -L "$OLD_HELPER" && "$(readlink -- "$OLD_HELPER")" == "$HELPER" ]]; then
        skip "$OLD_HELPER -> $HELPER"
    else
        ts="$(date +%Y%m%d-%H%M%S)"
        if [[ -f "$OLD_HELPER" && ! -L "$OLD_HELPER" ]]; then
            run install -d -m 700 -- "$ETC_DIR/backup"
            run cp -a -- "$OLD_HELPER" "$ETC_DIR/backup/lockscreen-auth-check.$ts"
        fi
        if ((DRY)); then run ln -sfn -- "$HELPER" "$OLD_HELPER"
        else
            ln -sfn -- "$HELPER" "$OLD_HELPER.orbit-new"
            mv -fT -- "$OLD_HELPER.orbit-new" "$OLD_HELPER"
            ok "$OLD_HELPER -> $HELPER"
        fi
    fi

    # 5. Root-readable config path.
    step "Login integration: /etc/orbit"
    run install -d -m 755 -- "$ETC_DIR"
    link "$ETC_DIR/config.json" "$TARGET_HOME/.config/orbit/config.json"
    run install -d -m 755 -- "$OLD_ETC_DIR"
    link "$OLD_ETC_DIR/config.json" "$ETC_DIR/config.json"

    # 6. PAM stacks, only now that the new binary is in place and works.
    step "Login integration: PAM stacks ($mode)"
    if ((!DRY)) && [[ ! -x "$HELPER" ]]; then die "$HELPER missing; refusing to edit PAM"; fi
    pam_plan_install "$mode"
    if ((${#PLAN_NAMES[@]})); then
        pam_apply_plan || die "PAM changes rolled back; login is unchanged"
    fi
    local f left="" planned=" ${PLAN_NAMES[*]} "
    for f in /etc/pam.d/*; do
        if [[ ! -f "$f" ]] || ! grep -qF -- "$OLD_HELPER" "$f"; then continue; fi
        ((DRY)) && [[ "$planned" == *" $(basename -- "$f") "* ]] && continue
        left+="$f "
    done
    [[ -z "$left" ]] || info "still naming $OLD_HELPER (works via the symlink): $left"

    # 7. Start the login screen only after the user's daemon (cold boot).
    if [[ -f /usr/lib/systemd/system/plasmalogin.service || -f /etc/systemd/system/plasmalogin.service ]]; then
        run install -d -m 755 -- "$DM_DROPIN_DIR"
        write_file "$DM_DROPIN" 644 "[Unit]
Wants=user@$TARGET_UID.service
After=user@$TARGET_UID.service"
        if [[ -f "$DM_DROPIN_DIR/override.conf" ]] && \
           [[ "$(<"$DM_DROPIN_DIR/override.conf")" == $'[Unit]\nWants=user@1000.service\nAfter=user@1000.service' ]]; then
            remove "$DM_DROPIN_DIR/override.conf"
        fi
        try systemctl daemon-reload
    fi
}

root_auth_revert() {   # password-only login; returns 1 if PAM still references the helper
    step "Login: restore password-only"
    pam_plan_revert
    if ((${#PLAN_NAMES[@]})); then
        pam_apply_plan || { warn "PAM revert rolled back"; return 1; }
    fi
    remove "$DM_DROPIN"
    if [[ -f "$DM_DROPIN_DIR/override.conf" ]] && \
       [[ "$(<"$DM_DROPIN_DIR/override.conf")" == $'[Unit]\nWants=user@1000.service\nAfter=user@1000.service' ]]; then
        remove "$DM_DROPIN_DIR/override.conf"
    fi
    if [[ -d "$DM_DROPIN_DIR" ]] && [[ -z "$(ls -A -- "$DM_DROPIN_DIR")" ]]; then run rmdir -- "$DM_DROPIN_DIR"; fi
    try systemctl daemon-reload
    if ((!DRY)); then
        local left; left="$(pam_refs_left)"
        if [[ -n "$left" ]]; then warn "still referenced by: $(echo "$left" | tr '\n' ' ')"; return 1; fi
    fi
    ok "login and lock screen use the password only"
}

root_uninstall() {
    if ! root_auth_revert; then
        warn "keeping $HELPER because PAM still uses it"
        return 1
    fi
    step "System: remove Orbit files"
    remove "$HELPER"
    if [[ -L "$OLD_HELPER" ]] || { [[ -f "$OLD_HELPER" ]] && grep -q 'CONFIG_PATHS' "$OLD_HELPER"; }; then
        remove "$OLD_HELPER"
    fi
    remove "$ETC_DIR/config.json" "$OLD_ETC_DIR/config.json"
    if [[ -d "$OLD_ETC_DIR" ]] && [[ -z "$(ls -A -- "$OLD_ETC_DIR")" ]]; then run rmdir -- "$OLD_ETC_DIR"; fi
    if ((PURGE)); then remove "$ETC_DIR"
    elif [[ -d "$ETC_DIR" ]] && [[ -z "$(ls -A -- "$ETC_DIR")" ]]; then run rmdir -- "$ETC_DIR"
    elif [[ -d "$ETC_DIR" ]]; then info "kept $ETC_DIR (PAM backups; --purge removes it)"; fi
    remove "$UDEV_RULE" "$OLD_UDEV_RULE" "$MODLOAD" "$CAM_MODPROBE" "$CAM_MODLOAD"
    try udevadm control --reload-rules
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'; then
        try ufw delete allow 1760/tcp
        try ufw delete allow 1760/udp
        try ufw delete allow 1762/udp
        for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
            try ufw delete allow from "$net" to any port 1760 proto tcp
            try ufw delete allow from "$net" to any port 1760 proto udp
            try ufw delete allow from "$net" to any port 1762 proto udp
        done
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        try firewall-cmd --permanent --remove-port=1760/tcp --remove-port=1760/udp --remove-port=1762/udp
        for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
            for port in 1760/tcp 1760/udp 1762/udp; do
                try firewall-cmd --permanent --remove-rich-rule="rule family=ipv4 source address=$net port port=${port%/*} protocol=${port#*/} accept"
            done
        done
        try firewall-cmd --reload
    fi
    info "lingering for $TARGET_USER left enabled (other user services may need it)"
}

set_target() {    # TARGET_USER -> TARGET_HOME TARGET_UID (no eval)
    local ent
    ent="$(getent passwd -- "$TARGET_USER")" || die "unknown user: $TARGET_USER"
    TARGET_HOME="$(cut -d: -f6 <<<"$ent")"
    TARGET_UID="$(cut -d: -f3 <<<"$ent")"
    [[ "$TARGET_UID" -ge 1000 ]] || die "refusing system user $TARGET_USER"
}

if [[ -n "$ROOT_STEP" ]]; then
    [[ $EUID == 0 ]] || die "--root-step must run as root"
    [[ -n "$TARGET_USER" ]] || die "--root-step needs --user"
    set_target
    case "$ROOT_STEP" in
        install)
            root_common
            if ((NO_PAM)); then skip "PAM integration (--no-pam)"; else root_auth_install "$MODE"; fi ;;
        uninstall) root_uninstall ;;
        revert-pam) root_auth_revert ;;
        *) die "unknown root step: $ROOT_STEP" ;;
    esac
    exit 0
fi

# =============================================================================
# User side
# =============================================================================
[[ $EUID != 0 ]] || die "run this as your normal user (it asks for sudo when needed)"
TARGET_USER="$(id -un)"
set_target
HOME_DIR="$TARGET_HOME"
CFG="$HOME_DIR/.config/orbit"
OLD_CFG="$HOME_DIR/.config/pc-authenticator"
DATA="$HOME_DIR/.local/share/orbit"
CACHE="$HOME_DIR/.cache/orbit"
OLD_CACHE="$HOME_DIR/.cache/pc-authenticator"
BIN="$HOME_DIR/.local/bin"
UNITS="$HOME_DIR/.config/systemd/user"
AUTOSTART="$HOME_DIR/.config/autostart"
APPS="$HOME_DIR/.local/share/applications"
SVCMENU="$HOME_DIR/.local/share/kio/servicemenus"
FONTS="$HOME_DIR/.local/share/fonts/orbit"
ICON_DST="$HOME_DIR/.local/share/icons/hicolor/scalable/apps/orbit.svg"
LOGIN_SHELL="$(basename -- "$(getent passwd -- "$TARGET_USER" | cut -d: -f7)")"

PKG_INSTALLED=() PKG_PRESENT=() PKG_FAILED=() OPT_MISSING=() NOTES=()

uctl() { try systemctl --user "$@"; }

root_step() {     # run a root step via sudo (or preview it in dry-run)
    local stepname="$1"
    if ((DRY)); then
        SUDO_PFX="sudo "
        case "$stepname" in
            install) root_common; if ((NO_PAM)); then skip "PAM integration (--no-pam)"; else root_auth_install "$MODE"; fi ;;
            uninstall) root_uninstall || true ;;
            revert-pam) root_auth_revert || true ;;
        esac
        SUDO_PFX=""
        return 0
    fi
    local -a args=(--root-step "$stepname" --user "$TARGET_USER")
    [[ -n "$MODE" ]] && args+=(--mode "$MODE")
    ((NO_PAM)) && args+=(--no-pam)
    ((PURGE)) && args+=(--purge)
    info "asking for sudo for the system part"
    sudo -- bash "$SELF" "${args[@]}"
}

# ---- prerequisites ----------------------------------------------------------
detect_pm() {
    local pm
    for pm in pacman apt-get dnf zypper; do
        if command -v "$pm" >/dev/null 2>&1; then echo "${pm%-get}"; return; fi
    done
    echo none
}

pkg_for() {       # pm need -> package name
    case "$1:$2" in
        pacman:python3) echo python ;;
        *:python3) echo python3 ;;
        pacman:pyqt6) echo python-pyqt6 ;;
        zypper:pyqt6) echo python3-PyQt6 ;;
        *:pyqt6) echo python3-pyqt6 ;;
        *:script) echo util-linux ;;
        pacman:pyte) echo python-pyte ;;
        *:pyte) echo python3-pyte ;;
        pacman:qdbus6) echo qt6-tools ;;
        apt:qdbus6) echo qdbus-qt6 ;;
        dnf:qdbus6) echo qt6-qttools ;;
        zypper:qdbus6) echo qt6-tools-qdbus ;;
        apt:notify-send) echo libnotify-bin ;;
        zypper:notify-send) echo libnotify-tools ;;
        *:notify-send) echo libnotify ;;
        pacman:pactl|pacman:paplay) echo libpulse ;;
        *:pactl|*:paplay) echo pulseaudio-utils ;;
        apt:spectacle) echo kde-spectacle ;;
        *:spectacle) echo spectacle ;;
        dnf:ffmpeg) echo ffmpeg-free ;;
        *:ffmpeg) echo ffmpeg ;;
        *:kdialog) echo kdialog ;;
        *:xdg-open) echo xdg-utils ;;
        dnf:ip) echo iproute ;;
        *:ip) echo iproute2 ;;
        *:fc-cache) echo fontconfig ;;
    esac
}

have() {
    case "$1" in
        pyqt6) python3 -c 'import PyQt6.QtWidgets' >/dev/null 2>&1 ;;
        pyte) python3 -c 'import pyte' >/dev/null 2>&1 ;;
        *) command -v "$1" >/dev/null 2>&1 ;;
    esac
}

step_prereqs() {
    step "Prerequisites"
    local pm need pkg
    local -a needs=(python3 pyqt6 pyte script qdbus6 notify-send pactl paplay spectacle ffmpeg kdialog xdg-open ip fc-cache)
    local -a missing_pkgs=()
    pm="$(detect_pm)"
    info "package manager: $pm"
    for need in "${needs[@]}"; do
        if have "$need"; then PKG_PRESENT+=("$need"); continue; fi
        pkg="$(pkg_for "$pm" "$need")"
        if [[ -z "$pkg" || "$pm" == none ]]; then PKG_FAILED+=("$need"); continue; fi
        [[ " ${missing_pkgs[*]} " == *" $pkg "* ]] || missing_pkgs+=("$pkg")
    done
    for need in systemctl loginctl busctl unix_chkpwd; do
        have "$need" || { PKG_FAILED+=("$need"); warn "$need not found (systemd / pam provide it)"; }
    done
    for need in wpctl kscreen-doctor kbuildsycoca6 fsearch-cli ngrok cloudflared; do
        have "$need" || [[ -x "$BIN/$need" ]] || OPT_MISSING+=("$need")
    done

    if ((${#missing_pkgs[@]} == 0)); then
        ok "all required tools present"
        return 0
    fi
    info "missing: ${missing_pkgs[*]}"
    local -a cmd
    case "$pm" in
        pacman) cmd=(pacman -S --needed) ; [[ -t 0 ]] || cmd+=(--noconfirm) ;;
        apt)    cmd=(apt-get install)    ; [[ -t 0 ]] || cmd+=(-y) ;;
        dnf)    cmd=(dnf install)        ; [[ -t 0 ]] || cmd+=(-y) ;;
        zypper) cmd=(zypper install)     ; [[ -t 0 ]] || cmd=(zypper --non-interactive install) ;;
    esac
    if ((DRY)); then
        SUDO_PFX="sudo "; run "${cmd[@]}" "${missing_pkgs[@]}"; SUDO_PFX=""
        PKG_INSTALLED+=("${missing_pkgs[@]}")
    elif sudo -- "${cmd[@]}" "${missing_pkgs[@]}"; then
        PKG_INSTALLED+=("${missing_pkgs[@]}")
        ok "installed: ${missing_pkgs[*]}"
    else
        PKG_FAILED+=("${missing_pkgs[@]}")
        warn "package install failed; continuing (some features will not work)"
    fi
}

# ---- stop old pieces --------------------------------------------------------
TRAY_PATTERN='python[0-9.]* [^ ]*(pc-connect-tray|orbit-tray)(\.py)?( |$)'
TRAY_UNIT_OLD='app-pc\x2dconnect\x2dtray@autostart.service'

step_stop_old() {
    step "Stop old services"
    if systemctl --user cat pc-authenticator.service >/dev/null 2>&1; then
        uctl disable --now pc-authenticator.service
        ((DRY)) || ok "stopped and disabled pc-authenticator.service"
    else
        skip "pc-authenticator.service not present"
    fi
    uctl disable --now pc-connect-tray.service
    uctl stop "$TRAY_UNIT_OLD"
    uctl reset-failed 'app-pc_connect@*' 'app-pc\x2dconnect*'
    if pgrep -u "$TARGET_UID" -f "$TRAY_PATTERN" >/dev/null 2>&1; then
        try pkill -u "$TARGET_UID" -f "$TRAY_PATTERN"
        ((DRY)) || ok "stopped running tray"
    fi
}

# ---- config migration -------------------------------------------------------
# Files the old installer copied into ~/.config/pc-authenticator. Safe to delete.
OBSOLETE=(daemon.py daemon.py.bak daemon.py.bak-pre-tmux setup.sh install.sh uninstall.sh
          revert-to-password.sh enroll-usb.py kdeconnect-handler lockscreen-auth-check
          pc-connect pc-connect-send pc-connect-tray.py pc-connect.desktop pc_connect.desktop
          pc-connect-tray.desktop pc-connect-tray.service kde-pam kde-pam-2fa kde-pam-passwordless
          sddm-pam-2fa sddm-pam-passwordless plasmalogin-pam-2fa plasmalogin-pam-passwordless
          kde-google-auth-pam setup-google-auth.sh disable-google-auth.sh enable-forward-pass.sh
          config.example.json tray.lock __pycache__ static)

step_migrate() {
    step "Config: ~/.config/orbit"
    if [[ -d "$OLD_CFG" && ! -L "$OLD_CFG" && ! -e "$CFG" ]]; then
        run mv -- "$OLD_CFG" "$CFG"
        run ln -s -- orbit "$OLD_CFG"
        ((DRY)) || ok "moved ~/.config/pc-authenticator -> ~/.config/orbit (old name is now a symlink)"
    elif [[ -d "$OLD_CFG" && ! -L "$OLD_CFG" && -d "$CFG" ]]; then
        warn "both ~/.config/pc-authenticator and ~/.config/orbit exist; using orbit"
        if [[ -f "$OLD_CFG/config.json" && ! -f "$CFG/config.json" ]]; then
            run cp -p -- "$OLD_CFG/config.json" "$CFG/config.json"
        fi
        NOTES+=("~/.config/pc-authenticator was left in place (both dirs existed)")
    elif [[ -L "$OLD_CFG" ]]; then
        skip "already migrated"
    fi
    run install -d -m 700 -- "$CFG"
    ((DRY)) || chmod 700 -- "$CFG"
    local dir="$CFG"; [[ -d "$CFG" ]] || dir="$OLD_CFG"   # dry-run: not moved yet
    if [[ -f "$dir/config.json" ]]; then
        if [[ "$(stat -c %a -- "$dir/config.json")" != 600 ]]; then run chmod 600 -- "$CFG/config.json"; fi
        ok "config.json kept (phone pairing preserved)"
    else
        run install -m 600 -- "$SRC/config.example.json" "$CFG/config.json"
        ((DRY)) || ok "created config.json"
    fi
    # The staging dir may hold files waiting for the phone; only drop it if empty.
    if [[ -d "$dir/staging" ]] && [[ -z "$(ls -A -- "$dir/staging")" ]]; then remove "$CFG/staging"; fi
    local f
    for f in "${OBSOLETE[@]}"; do
        if [[ -e "$dir/$f" || -L "$dir/$f" ]]; then run rm -rf -- "${CFG:?}/$f"; fi
    done
    local leftover="" e b
    for e in "$dir"/* "$dir"/.[!.]*; do
        [[ -e "$e" || -L "$e" ]] || continue
        b="$(basename -- "$e")"
        case " config.json local_token staging ${OBSOLETE[*]} " in *" $b "*) continue ;; esac
        leftover+="$b "
    done
    [[ -z "$leftover" ]] || info "left untouched in ~/.config/orbit: $leftover"

    step "Cache: ~/.cache/orbit"
    if [[ -d "$OLD_CACHE" && ! -L "$OLD_CACHE" ]]; then
        mkd 700 "$CACHE"
        local e
        for e in "$OLD_CACHE"/* "$OLD_CACHE"/.[!.]*; do
            [[ -e "$e" ]] || continue
            if [[ -e "$CACHE/$(basename -- "$e")" ]]; then continue; fi
            run mv -- "$e" "$CACHE/"
        done
        if ((DRY)) || [[ -z "$(ls -A -- "$OLD_CACHE")" ]]; then
            run rmdir -- "$OLD_CACHE"
        else
            info "kept $OLD_CACHE (has entries that also exist in ~/.cache/orbit)"
        fi
    else
        mkd 700 "$CACHE"
        skip "nothing to migrate"
    fi
}

# ---- files ------------------------------------------------------------------
subst() {         # template -> content with @BINDIR@ / @ICON@ filled in
    local icon=smartphone
    [[ -f "$SRC/icons/orbit.svg" ]] && icon=orbit
    sed -e "s|@BINDIR@|$BIN|g" -e "s|@ICON@|$icon|g" -- "$1"
}

# put_exec SRC DEST  -- install an executable by renaming over the old one, so terminals
# still running the old copy keep working (writing into a running binary fails)
put_exec() {
    if ((DRY)); then run install -m 755 -- "$1" "$2"; return 0; fi
    local tmp
    tmp="$(mktemp -- "$(dirname -- "$2")/.orbit-tmp.XXXXXX")"
    install -m 755 -- "$1" "$tmp" && mv -f -- "$tmp" "$2"
}

# The C++ parts (native/): the terminal recorder (orbit-term, ~0.3 MB per open terminal instead
# of ~6 MB for Python) and the service's terminal emulator (liborbitvt.so, rebuilds a terminal's
# history many times faster than pyte). Built here when a C++ compiler is around; without one,
# the Python versions do the same job, slower.
build_native() {
    local cxx="" c out
    NATIVE_OK=0
    for c in c++ g++ clang++; do
        if command -v "$c" >/dev/null 2>&1; then cxx="$c"; break; fi
    done
    if [[ -z "$cxx" ]]; then
        warn "no C++ compiler (g++ or clang++): using the Python terminal recorder and emulator"
        NOTES+=("Install g++ and re-run for the faster terminal recorder and history (C++)")
        return 0
    fi
    if ((DRY)); then
        run "$cxx" -std=c++17 -O2 -o "$BIN/orbit-term" "$SRC/native/orbit-term.cpp"
        run "$cxx" -std=c++17 -O2 -shared -fPIC -fvisibility=hidden -o "$DATA/liborbitvt.so" "$SRC/native/orbitvt.cpp"
        NATIVE_OK=1
        return 0
    fi
    out="$(mktemp -d)"
    if "$cxx" -std=c++17 -O2 -o "$out/orbit-term" "$SRC/native/orbit-term.cpp" 2>"$out/err" &&
       "$cxx" -std=c++17 -O2 -shared -fPIC -fvisibility=hidden -o "$out/liborbitvt.so" "$SRC/native/orbitvt.cpp" 2>>"$out/err"; then
        put_exec "$out/orbit-term" "$BIN/orbit-term"
        put_exec "$out/liborbitvt.so" "$DATA/liborbitvt.so"
        NATIVE_OK=1
        ok "built with $cxx: orbit-term, liborbitvt.so"
    else
        warn "C++ build failed: $(grep -m1 error "$out/err"); using the Python versions"
    fi
    rm -rf -- "$out"
}

step_files() {
    step "Program files: ~/.local/share/orbit"
    [[ -f "$SRC/orbitd.py" ]] || die "linux/orbitd.py missing"
    mkd 755 "$DATA"
    run install -m 755 -- "$SRC/orbitd.py" "$DATA/orbitd.py"
    remove "$DATA/daemon.py"     # the service's old name
    run install -m 755 -- "$SRC/enroll-usb.py" "$DATA/enroll-usb.py"
    remove "$DATA/static.new"
    run cp -a -- "$SRC/static" "$DATA/static.new"
    remove "$DATA/static"
    run mv -- "$DATA/static.new" "$DATA/static"
    ((DRY)) || ok "daemon, enroll-usb.py, static/ installed"

    step "Commands: ~/.local/bin"
    mkd 755 "$BIN"
    run install -m 755 -- "$SRC/orbit" "$BIN/orbit"
    run install -m 755 -- "$SRC/orbit-send" "$BIN/orbit-send"
    build_native
    if ((!NATIVE_OK)); then
        put_exec "$SRC/orbit-term" "$BIN/orbit-term"
        remove "$DATA/liborbitvt.so"
    fi
    run install -m 755 -- "$SRC/kdeconnect-handler" "$BIN/kdeconnect-handler"
    local tray_ok=0
    if [[ -f "$SRC/orbit-tray.py" ]]; then
        run install -m 755 -- "$SRC/orbit-tray.py" "$BIN/orbit-tray"; tray_ok=1
    else
        warn "linux/orbit-tray.py not found; orbit-tray not installed"
        NOTES+=("orbit-tray was not installed (linux/orbit-tray.py missing); re-run later")
    fi
    ((DRY)) || ok "orbit, orbit-send, kdeconnect-handler$([[ $tray_ok == 1 ]] && echo ', orbit-tray')"
    link "$BIN/pc-connect" orbit
    link "$BIN/pc-connect-send" orbit-send
    if ((tray_ok)); then
        link "$BIN/pc-connect-tray.py" orbit-tray
        link "$BIN/pc-connect-tray" orbit-tray
    fi
    if [[ -L "$BIN/pc-auth" ]] && [[ "$(readlink -- "$BIN/pc-auth")" == *pc-authenticator* ]]; then
        remove "$BIN/pc-auth"
    fi

    step "Desktop integration"
    mkd 755 "$APPS" "$SVCMENU" "$AUTOSTART"
    write_file "$APPS/orbit.desktop" 644 "$(subst "$SRC/orbit.desktop")"
    write_file "$SVCMENU/orbit-send.desktop" 755 "$(subst "$SRC/orbit-send.desktop")"
    remove "$APPS/pc-connect.desktop" "$APPS/pc_connect.desktop" "$SVCMENU/pc_connect.desktop" "$SVCMENU/pc-connect.desktop"
    if [[ -f "$SRC/orbit-tray.desktop" ]]; then
        # absolute Exec: the session PATH may not include ~/.local/bin
        write_file "$AUTOSTART/orbit-tray.desktop" 644 \
            "$(sed -E "s|^Exec=orbit-tray|Exec=$BIN/orbit-tray|" -- "$SRC/orbit-tray.desktop")"
    else
        warn "linux/orbit-tray.desktop not found; tray autostart not installed"
    fi
    remove "$AUTOSTART/pc-connect-tray.desktop"
    remove "$UNITS/pc-connect-tray.service"
    local leftover
    for leftover in "$UNITS"/app-pc*connect*tray* "$UNITS"/*.wants/pc-connect-tray.service; do
        [[ -e "$leftover" || -L "$leftover" ]] && remove "$leftover"
    done

    if [[ -f "$SRC/icons/orbit.svg" ]]; then
        run install -D -m 644 -- "$SRC/icons/orbit.svg" "$ICON_DST"
        ((DRY)) || ok "icon installed"
    else
        skip "linux/icons/orbit.svg not present (using a stock icon)"
    fi

    local -a fonts=()
    local f
    for f in "$REPO"/android/res/font/geist_*.ttf; do [[ -f "$f" ]] && fonts+=("$f"); done
    if ((${#fonts[@]})); then
        mkd 755 "$FONTS"
        run install -m 644 -- "${fonts[@]}" "$FONTS/"
        try fc-cache -f "$FONTS"
        ((DRY)) || ok "${#fonts[@]} Geist fonts installed"
    else
        warn "no android/res/font/geist_*.ttf found"
    fi
    if command -v kbuildsycoca6 >/dev/null 2>&1; then try kbuildsycoca6; fi
    if command -v update-desktop-database >/dev/null 2>&1; then try update-desktop-database "$APPS"; fi
}

# ---- systemd user service ---------------------------------------------------
UNIT_CONTENT='[Unit]
Description=Orbit (phone and computer link)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/.local/share/orbit/orbitd.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target'

step_service() {
    step "Background service: orbit.service"
    mkd 755 "$UNITS"
    write_file "$UNITS/orbit.service" 644 "$UNIT_CONTENT"
    remove "$UNITS/pc-authenticator.service" "$UNITS/default.target.wants/pc-authenticator.service"
    uctl daemon-reload
    uctl enable orbit.service
    uctl restart orbit.service
    if ((DRY)); then return 0; fi
    local i
    for i in 1 2 3 4 5; do
        if systemctl --user is-active --quiet orbit.service; then ok "orbit.service is running"; return 0; fi
        sleep 1
    done
    warn "orbit.service is not active; see: journalctl --user -u orbit.service"
    NOTES+=("orbit.service did not start; check journalctl --user -u orbit.service")
}

# ---- terminal history (shell recording) -----------------------------------
# Every interactive terminal (Konsole, kitty, VS Code, ssh, ...) runs its shell under
# orbit-term, which records the output (and resizes) to ~/.cache/orbit/term/<pid>.* for the
# phone, and takes the phone's typing through a socket beside it. The terminal itself is
# untouched: same colours, scrollback and selection. tmux panes are skipped (read directly).
FISH_BLOCK='# Terminals record their output so the Orbit phone app can show and type into them.
# Nothing changes in the terminal itself. NO_ORBIT_RECORDING=1 skips it.
if status is-interactive; and isatty stdin; and not set -q ORBIT_RECORDING; and not set -q NO_ORBIT_RECORDING; and not set -q TMUX; and test "$TERM" != dumb; and command -q orbit-term
    exec orbit-term (status fish-path)
end

# The terminal only sees orbit-term, so name the tab the way it would: "folder : command"
if status is-interactive; and set -q ORBIT_RECORDING
    function fish_title
        echo (path basename $PWD) : (status current-command)
    end
end'

SH_BLOCK='# Terminals record their output so the Orbit phone app can show and type into them.
# Nothing changes in the terminal itself. NO_ORBIT_RECORDING=1 skips it.
if [[ $- == *i* && -t 0 && -t 1 && -z "${ORBIT_RECORDING:-}" && -z "${NO_ORBIT_RECORDING:-}" && -z "${TMUX:-}" && "${TERM:-}" != dumb ]] && command -v orbit-term >/dev/null 2>&1; then
    if [[ -n "${ZSH_VERSION:-}" ]]; then exec orbit-term "$(command -v zsh)"; else exec orbit-term "${BASH:-bash}"; fi
fi'

# render_block FILE KIND BLOCK -> new file content on stdout.
# KIND: tmux | fish | sh | remove. Replaces the marked block in place, else the
# old un-marked Orbit block (if it is clearly ours), else appends.
render_block() {
    local file="$1" kind="$2" block="$3" content=""
    [[ -f "$file" ]] && content="$(<"$file")"
    printf '%s\n' "$content" | KIND="$kind" BLOCK="$block" B="$MARK_BEGIN" E="$MARK_END" awk '
        function emit() { if (done || ENVIRON["KIND"] == "remove") { done = 1; return }
                          out[++m] = ENVIRON["B"]; out[++m] = ENVIRON["BLOCK"]; out[++m] = ENVIRON["E"]; done = 1 }
        { line[NR] = $0 }
        END {
            n = NR; if (n == 1 && line[1] == "") n = 0
            # legacy (un-marked) Orbit blocks: ranges [rs[k], re[k]]
            nr = 0; skipm = 0
            for (i = 1; i <= n; i++) {
                if (line[i] == ENVIRON["B"]) { skipm = 1; continue }
                if (skipm) { if (line[i] == ENVIRON["E"]) skipm = 0; continue }
                if (line[i] !~ /^#/) continue
                fishhdr = index(line[i], "Run Konsole tabs inside tmux") || index(line[i], "Inside tmux fish can")
                if (ENVIRON["KIND"] != "tmux" && fishhdr) {
                    for (j = i + 1; j <= n; j++) {
                        if (line[j] ~ /^end[ \t]*$/) { rs[++nr] = i; re[nr] = j; i = j; break }
                        if (!(line[j] ~ /^[ \t]*#/ || index(line[j], "tmux") || index(line[j], "TMUX") || index(line[j], "COLORTERM") || index(line[j], "fish_config theme"))) break
                    }
                    continue
                }
                if ((ENVIRON["KIND"] == "tmux" || ENVIRON["KIND"] == "remove") && index(line[i], "Konsole tabs run inside tmux")) {
                    le = i
                    for (j = i + 1; j <= n; j++) {
                        if (line[j] ~ /^[ \t]*(#|set |set-option |setw |unbind |bind )/) le = j; else break
                    }
                    rs[++nr] = i; re[nr] = le; i = le
                }
            }
            inm = 0; k = 1
            for (i = 1; i <= n; i++) {
                if (line[i] == ENVIRON["B"]) { emit(); inm = 1; continue }
                if (inm) { if (line[i] == ENVIRON["E"]) inm = 0; continue }
                if (k <= nr && i == rs[k]) {
                    emit(); i = re[k]; k++; continue
                }
                if (line[i] == "" && k <= nr && i + 1 == rs[k] && done) continue
                out[++m] = line[i]
            }
            if (!done && ENVIRON["KIND"] != "remove") {
                if (m > 0 && out[m] != "") out[++m] = ""
                emit()
            }
            if (ENVIRON["KIND"] == "remove") while (m > 0 && out[m] == "") m--
            for (i = 1; i <= m; i++) print out[i]
        }'
}

apply_block() {   # file kind block label
    local file="$1" kind="$2" block="$3" label="$4" real new
    real="$file"; [[ -L "$file" ]] && real="$(readlink -f -- "$file")"
    if [[ "$kind" == remove && ! -f "$real" ]]; then return 0; fi
    new="$(render_block "$real" "$kind" "$block")"
    if [[ -f "$real" && "$new" == "$(<"$real")" ]]; then skip "$label unchanged"; return 0; fi
    if [[ -f "$real" && ! -e "$real.pre-orbit" ]]; then run cp -p -- "$real" "$real.pre-orbit"; fi
    if ((DRY)) && [[ -f "$real" ]]; then
        diff -u --label "$label (now)" --label "$label (after)" "$real" <(printf '%s\n' "$new") | sed 's/^/      /' || true
    fi
    # never chmod an existing dir (e.g. a 700 home)
    [[ -d "$(dirname -- "$real")" ]] || run mkdir -p -- "$(dirname -- "$real")"
    write_file "$real" "$( [[ -f "$real" ]] && stat -c %a -- "$real" || echo 644)" "$new"
}

konsole_tab_titles() {   # let the shell name Konsole tabs (Konsole itself only sees `script`)
    local rc="$HOME_DIR/.config/konsolerc" name prof
    name="$(sed -n 's/^DefaultProfile=//p' "$rc" 2>/dev/null | head -n1)"
    [[ -n "$name" ]] || { skip "no default Konsole profile set"; return 0; }
    prof="$HOME_DIR/.local/share/konsole/$name"
    [[ -f "$prof" ]] || { skip "Konsole profile $name not found"; return 0; }
    if grep -q '^LocalTabTitleFormat=' "$prof"; then skip "Konsole tab titles already customised"; return 0; fi
    [[ -e "$prof.pre-orbit" ]] || run cp -p -- "$prof" "$prof.pre-orbit"
    write_file "$prof" 644 "$(awk '{print} /^\[General\]$/ && !d {print "LocalTabTitleFormat=%w"; d=1}' "$prof")"
}

step_term_history() {
    step "Terminal history (recording)"
    if ((NO_TMUX)); then skip "--no-term-history"; return 0; fi
    # every shell that is installed, not just the login shell: any of them can open a terminal
    local any=0
    if command -v fish >/dev/null 2>&1 || [[ "$LOGIN_SHELL" == fish ]]; then
        apply_block "$HOME_DIR/.config/fish/config.fish" fish "$FISH_BLOCK" "config.fish"; any=1
    fi
    if command -v bash >/dev/null 2>&1; then apply_block "$HOME_DIR/.bashrc" sh "$SH_BLOCK" "~/.bashrc"; any=1; fi
    if command -v zsh >/dev/null 2>&1; then apply_block "$HOME_DIR/.zshrc" sh "$SH_BLOCK" "~/.zshrc"; any=1; fi
    ((any)) || warn "no fish, bash or zsh found; phone terminals need one of them"
    konsole_tab_titles
    info "new terminals record their output for the phone (set NO_ORBIT_RECORDING=1 to skip)"
}

# ---- tray -------------------------------------------------------------------
step_start_tray() {
    [[ -x "$BIN/orbit-tray" ]] || ((DRY)) || return 0
    [[ -f "$SRC/orbit-tray.py" ]] || return 0
    if [[ -z "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]]; then skip "no graphical session; tray starts at next login"; return 0; fi
    step "Tray"
    local unit
    unit="app-$(systemd-escape -- orbit-tray)@autostart.service"
    if run systemctl --user start "$unit" 2>/dev/null; then
        ((DRY)) || ok "started $unit"
    else
        run setsid -f "$BIN/orbit-tray" >/dev/null 2>&1 </dev/null
        ((DRY)) || ok "started orbit-tray"
    fi
}

# ---- mode -------------------------------------------------------------------
resolve_mode() {
    ((NO_PAM)) && return 0
    local cur; cur="$(detect_pam_mode)"
    if [[ -n "$MODE" ]]; then info "unlock mode: $MODE (current: $cur)"; return 0; fi
    if [[ "$cur" != none ]]; then MODE="$cur"; info "unlock mode: keeping $MODE"; return 0; fi
    MODE=2fa
    if [[ -t 0 ]] && ((!DRY)); then
        printf '\n  Unlock mode for login and lock screen:\n'
        printf '    1) 2fa           password, then approve on the phone (default)\n'
        printf '    2) passwordless  empty password + Enter, approve on the phone\n'
        local ans; read -rp "  Choose [1/2]: " ans || true
        [[ "$ans" == 2 ]] && MODE=passwordless
    fi
    info "unlock mode: $MODE"
}

summary() {
    step "Summary"
    ((${#PKG_INSTALLED[@]})) && printf '  %s %s\n' "$( ((DRY)) && echo 'would install:' || echo 'installed:   ')" "${PKG_INSTALLED[*]}"
    ((${#PKG_PRESENT[@]})) && printf '  already had:  %s\n' "${PKG_PRESENT[*]}"
    ((${#PKG_FAILED[@]})) && printf '  %smissing:      %s%s\n' "$C_YEL" "${PKG_FAILED[*]}" "$C_OFF"
    ((${#OPT_MISSING[@]})) && printf '  optional, not installed: %s\n' "${OPT_MISSING[*]}"
    printf '  unlock:       %s\n' "$( ((NO_PAM)) && echo 'unchanged (--no-pam)' || echo "$MODE")"
    printf '  terminal history: %s\n' "$( ((NO_TMUX)) && echo skipped || echo "on ($LOGIN_SHELL)")"
    local n; for n in "${NOTES[@]}"; do printf '  %snote:%s %s\n' "$C_YEL" "$C_OFF" "$n"; done
    if ((DRY)); then printf '\n  %sDry run: nothing was changed.%s\n' "$C_B" "$C_OFF"
    else printf '\n  %sDone.%s Try: orbit status   (uninstall: %s --uninstall)\n' "$C_B" "$C_OFF" "$SELF"; fi
}

# ---- uninstall (user side) --------------------------------------------------
do_uninstall() {
    printf '%sOrbit uninstall%s%s\n' "$C_B" "$C_OFF" "$( ((DRY)) && echo ' (dry run)')"
    # Root first: PAM must stop calling the helper before anything else goes.
    root_step uninstall || die "system part failed; nothing else removed"

    step "Background service and tray"
    uctl disable --now orbit.service
    uctl disable --now pc-authenticator.service
    uctl stop "app-$(systemd-escape -- orbit-tray)@autostart.service" "$TRAY_UNIT_OLD"
    if pgrep -u "$TARGET_UID" -f "$TRAY_PATTERN" >/dev/null 2>&1; then try pkill -u "$TARGET_UID" -f "$TRAY_PATTERN"; fi
    remove "$UNITS/orbit.service" "$UNITS/pc-authenticator.service" "$UNITS/pc-connect-tray.service" \
           "$UNITS/default.target.wants/orbit.service" "$UNITS/default.target.wants/pc-authenticator.service"
    uctl daemon-reload

    step "Files"
    remove "$DATA" "$CACHE" "$OLD_CACHE" "$FONTS" "$ICON_DST"
    remove "$BIN/orbit" "$BIN/orbit-send" "$BIN/orbit-tray" "$BIN/orbit-term"
    local l
    for l in pc-connect pc-connect-send pc-connect-tray pc-connect-tray.py pc-auth; do
        [[ -L "$BIN/$l" ]] && remove "$BIN/$l"
    done
    if [[ -f "$BIN/kdeconnect-handler" ]] && grep -q 'orbit-send\|pc-connect-send' "$BIN/kdeconnect-handler"; then
        remove "$BIN/kdeconnect-handler"
    fi
    remove "$APPS/orbit.desktop" "$APPS/pc-connect.desktop" "$SVCMENU/orbit-send.desktop" "$SVCMENU/pc_connect.desktop" \
           "$AUTOSTART/orbit-tray.desktop" "$AUTOSTART/pc-connect-tray.desktop"
    command -v fc-cache >/dev/null 2>&1 && try fc-cache -f
    command -v kbuildsycoca6 >/dev/null 2>&1 && try kbuildsycoca6

    step "Terminal setup"
    apply_block "$HOME_DIR/.config/fish/config.fish" remove "" "config.fish"
    apply_block "$HOME_DIR/.bashrc" remove "" "~/.bashrc"
    apply_block "$HOME_DIR/.zshrc" remove "" "~/.zshrc"

    step "Config"
    if ((PURGE)); then
        remove "$CFG"
        [[ -L "$OLD_CFG" ]] && remove "$OLD_CFG"
        [[ -d "$OLD_CFG" ]] && remove "$OLD_CFG"
    else
        remove "$CFG/local_token"
        info "kept ~/.config/orbit/config.json (phone pairing). --purge deletes it."
    fi
    printf '\n  %s%s%s\n' "$C_B" "$( ((DRY)) && echo 'Dry run: nothing was changed.' || echo 'Orbit removed. Login uses your password only.')" "$C_OFF"
}

# ---- main -------------------------------------------------------------------
case "$ACTION" in
    uninstall) do_uninstall ;;
    revert-pam)
        printf '%sOrbit: restore password-only login%s\n' "$C_B" "$C_OFF"
        root_step revert-pam ;;
    install)
        printf '%sOrbit installer%s%s\n' "$C_B" "$C_OFF" "$( ((DRY)) && echo ' (dry run: nothing will change)')"
        info "source: $SRC"
        info "user: $TARGET_USER ($HOME_DIR), shell: $LOGIN_SHELL"
        resolve_mode
        step_prereqs
        step_stop_old
        step_migrate
        step_files
        step_service
        step_term_history
        root_step install
        step_start_tray
        summary ;;
esac
