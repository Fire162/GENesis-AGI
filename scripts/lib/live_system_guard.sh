# shellcheck shell=bash
# Live-system guard for bootstrap.sh — sourced, not executed.
#
# bootstrap_refuse_if_server_live [args...]
#
# bootstrap.sh is a setup/repair tool, NOT a deploy path. Running it on a
# machine with a live genesis-server re-runs installers (curl|bash, npm -g,
# pip), re-registers MCP servers + systemd units, and edits dotfiles on top
# of a running system — the direct trigger of the 2026-07-13 eth0/load
# incident. Code changes deploy via scripts/update.sh, or scripts/deploy_code_only.sh
# for a code-only change (a locked pull + restart, serialized with update.sh).
#
# This function makes that rule mechanical: refuse to proceed while
# genesis-server is live, unless one of two overrides applies:
#
#   GENESIS_BOOTSTRAP_ALLOW_LIVE=1  — sanctioned-caller opt-out. update.sh
#       sets it on its bootstrap invocation: its ERR trap is armed there, so
#       a guard refusal inside update.sh would trigger a FULL update rollback
#       (git reset to the rollback tag) — the guard must never gate the
#       sanctioned deploy path, which stops the server itself beforehand.
#   --force  — human override for deliberate live repair. Proceeds with a
#       loud warning.
#
# Server-live detection mirrors update.sh's _ensure_server_down check so the
# two can't drift: live iff `systemctl --user is-active` reports active OR a
# `python -m genesis serve` process exists. Detection is FAIL-OPEN: any other
# outcome (unknown unit rc=4, no session D-Bus, pgrep miss/absent) reads as
# not-running and bootstrap proceeds — a broken detector must never block a
# fresh-install bootstrap (where no server exists yet).
#
# Return codes (caller decides severity, cf. venv_setup.sh):
#   0 — proceed (not live, opt-out set, or --force given; warning printed
#       when forcing past a live server)
#   1 — refused: server is live, no override (message already printed)

_genesis_server_is_live() {
    if systemctl --user is-active --quiet genesis-server.service 2>/dev/null; then
        return 0
    fi
    if pgrep -f "python -m genesis serve" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

bootstrap_refuse_if_server_live() {
    # Sanctioned-caller opt-out (update.sh deploy path) — silent, unconditional.
    if [ -n "${GENESIS_BOOTSTRAP_ALLOW_LIVE:-}" ]; then
        return 0
    fi

    local force=false arg
    for arg in "$@"; do
        [ "$arg" = "--force" ] && force=true
    done

    if ! _genesis_server_is_live; then
        return 0
    fi

    if [ "$force" = "true" ]; then
        echo "WARNING: proceeding on a LIVE system by --force (genesis-server is running)."
        return 0
    fi

    cat >&2 <<'EOF'
REFUSED: genesis-server is RUNNING — bootstrap.sh is a setup/repair tool,
not a deploy path. Running it on a live system re-runs installers and
re-registers services on top of a running server (this triggered the
2026-07-13 eth0/load incident).

To deploy code changes, use the sanctioned path instead:
    scripts/update.sh
  or, for a code-only change (locked pull + restart, serialized with update.sh):
    scripts/deploy_code_only.sh

To run bootstrap for deliberate live repair anyway:
    ./scripts/bootstrap.sh --force
EOF
    return 1
}

# refuse_if_test_fenced_live <acting-root>
#
# The suite-wide test fence (tests/_live_fence.py) exports
# GENESIS_TEST_FENCE_LIVE_ROOTS: a colon-separated list of real paths no test
# may let a deploy script act on (this checkout, the real ~/.genesis and
# ~/genesis). The decision lives HERE, inside the script, because only the
# script knows the root it will act on after bash's own expansion — no amount
# of argv or shell-string prediction from outside converges (BASH_ENV,
# --rcfile, nested -c, a path built in a variable all defeat it).
#
#   unset/empty  → return 0 immediately: production behaviour never changes.
#   set          → realpath -m the acting root, $HOME and ${GENESIS_HOME:-};
#                  any of them equal to or under a listed root prints
#                  "REFUSED (test fence): <script> would act on <path>" to
#                  stderr, appends the same line to $GENESIS_TEST_FENCE_LOG
#                  when set (so the offending test fails at teardown), and
#                  returns 1.
#
# Callers run `refuse_if_test_fenced_live "$ROOT" || exit 97` as the FIRST
# statement after resolving the root they act on, before any side effect —
# 97 so a fenced refusal can't be mistaken for an ordinary script failure.

refuse_if_test_fenced_live() {
    [ -n "${GENESIS_TEST_FENCE_LIVE_ROOTS:-}" ] || return 0
    local _candidate _listed _hit=""
    # Split into an array with `read`: unquoted expansion in `for` would glob —
    # a literal root like /tmp/live[1] must compare as itself, not its matches.
    local -a _roots=()
    IFS=: read -ra _roots <<< "$GENESIS_TEST_FENCE_LIVE_ROOTS"
    for _candidate in "$1" "${HOME:-}" "${GENESIS_HOME:-}"; do
        [ -n "$_candidate" ] || continue
        _candidate="$(realpath -m "$_candidate" 2>/dev/null)" || continue
        for _listed in "${_roots[@]}"; do
            [ -n "$_listed" ] || continue
            # "${_listed%/}/" * covers the listed-root-is-/ case: "/" trims to
            # "" so the pattern is /* — every absolute descendant.
            if [ "$_candidate" = "$_listed" ] || [[ "$_candidate" == "${_listed%/}/"* ]]; then
                _hit="$_candidate"
                break 2
            fi
        done
    done
    [ -n "$_hit" ] || return 0
    # Name the script that sourced this lib (BASH_SOURCE[1]), not $0: under a
    # BASH_ENV/--rcfile launch $0 is "bash" while the script still names itself.
    local _script="${BASH_SOURCE[1]:-$0}"
    local _msg="REFUSED (test fence): ${_script##*/} would act on $_hit"
    printf '%s\n' "$_msg" >&2
    if [ -n "${GENESIS_TEST_FENCE_LOG:-}" ]; then
        printf '%s\n' "$_msg" >> "$GENESIS_TEST_FENCE_LOG" 2>/dev/null || true
    fi
    return 1
}
