# shellcheck shell=bash
# deploy_checkout.sh — the one answer to "may a deploy touch this checkout?"
#
# Sourced by scripts/deploy_code_only.sh and scripts/update.sh, so the two deploy
# paths cannot disagree about what a deployable checkout is, which tracked edits
# block a deploy, or which untracked files an incoming range would overwrite. The
# checks were first written inline in deploy_code_only.sh and are lifted here with
# their logic unchanged; the callers keep their own messages and their own order.
#
# Every function is a silent predicate: it RETURNS a status (and sets or prints
# what the caller needs) and never prints a refusal or exits. update.sh's EXIT trap
# removes its own temp copy by a readonly path, and an `exit` from inside a sourced
# function is the shape that trap was hardened against. Needs deploy_marker.sh
# (EPHEMERAL_DIRTY_RE) sourced first.

# The branch a deploy merges from upstream and must be standing on. Defined ONCE,
# here. Not configurable: a configurable deploy branch is a separate change.
DEPLOY_BRANCH=main

# Set _git_dir (this checkout's own git directory) and _common_dir (the shared one,
# as a physical path) for <root>. Either is empty when git cannot answer.
genesis_checkout_git_dirs() {
    local root="$1"
    _git_dir="$(git -C "$root" rev-parse --absolute-git-dir 2>/dev/null || true)"
    _common_dir="$(unset CDPATH; cd -- "$root" 2>/dev/null && cd -- "$(git rev-parse --git-common-dir 2>/dev/null || echo /nonexistent)" 2>/dev/null && pwd -P || true)"
}

# Is <root> a primary working tree — not a linked worktree, not a bare repository,
# not a .git directory? <git_dir> and <common_dir> are what genesis_checkout_git_dirs
# set. Git answers "linked worktree?" (the per-worktree git dir differs from the
# shared one), so a worktree added at ANY path is caught; the path arms stay for a
# plain clone parked under a worktree directory, which git calls primary. A bare
# repository and a root pointed at .git both pass the git-dir comparison, and
# `rev-parse --is-inside-work-tree` exits 0 for them while PRINTING "false", so the
# printed value is what decides.
genesis_is_primary_checkout() {
    local root="$1" git_dir="$2" common_dir="$3"
    [ -n "$git_dir" ] && [ -n "$common_dir" ] || return 1
    [ "$(unset CDPATH; cd -- "$git_dir" 2>/dev/null && pwd -P)" = "$common_dir" ] || return 1
    [[ "$root" == *"/.claude/worktrees/"* ]] && return 1
    [[ "$root" == *"/.worktrees/"* ]] && return 1
    [ "$(git -C "$root" rev-parse --is-inside-work-tree 2>/dev/null || true)" = true ]
}

# Is <root> standing on $DEPLOY_BRANCH? Sets _branch (empty on a detached HEAD).
#   --allow-override  honour GENESIS_ALLOW_NON_DEPLOY_BRANCH=1, which admits another
#                     NAMED branch. Never a detached HEAD, and never `live`: the
#                     override cannot admit the integration branch, which a caller
#                     admits only through genesis_live_checkout below, for the modes
#                     that do not pull into it. Only update.sh passes it.
# The caller tells an override admission from a plain one by comparing _branch with
# $DEPLOY_BRANCH, and says so itself.
genesis_deploy_branch_ok() {
    local root="$1" allow_override=false
    [ "${2:-}" = "--allow-override" ] && allow_override=true
    _branch="$(git -C "$root" symbolic-ref --short -q HEAD 2>/dev/null || true)"
    [ "$_branch" = "$DEPLOY_BRANCH" ] && return 0
    $allow_override || return 1
    [ "${GENESIS_ALLOW_NON_DEPLOY_BRANCH:-0}" = 1 ] || return 1
    [ -n "$_branch" ] && [ "$_branch" != live ]
}

# Is <root> on `live`, the integration branch scripts/deploy_candidates builds
# from the deploy manifest? Returns 0 (live), 1 (no: the branch rule above
# applies) or 2 (on `live` but the manifest or git cannot be read: refuse).
# Runs the text of scripts/lib/live_checkout.py that the CALLER read into
# $_LIVE_CHECKOUT_PY at startup, so the copy is the one this run started with;
# an empty or missing copy answers 2. The printed word must agree with the exit
# code (python itself exits 1 on a SyntaxError or an uncaught exception), so a
# broken copy answers 2, never 1. Prints nothing.
genesis_live_checkout() {
    local root="$1" rc=0 out
    [ -n "${_LIVE_CHECKOUT_PY:-}" ] || return 2
    out="$(python3 -I -S -c "$_LIVE_CHECKOUT_PY" "$root" 2>/dev/null)" || rc=$?
    case "$rc:$out" in
        "0:live") return 0 ;;
        "1:other") return 1 ;;
        *) return 2 ;;
    esac
}

# Tracked, locally modified paths that are NOT on the ephemeral allowlist, as
# porcelain lines; nothing when the tree is deployable. Returns 2 when the status
# cannot be read: it is read on its own first, because in a pipeline its failure
# would be swallowed and an unreadable status would pass as a clean tree.
# --no-renames: a rename is one line naming BOTH paths, so a tracked file renamed
# INTO an excused path would be excused whole; split, the deletion of the old path
# shows. A local change behind assume-unchanged or skip-worktree counts too: git
# status hides both, so without the second read a deploy, or a restart after a
# rollback, would run on code nobody can see (measured, git 2.43).
genesis_tracked_dirty_paths() {
    local root="$1" st hidden
    st="$(git -C "$root" status --porcelain --no-renames)" || return 2
    hidden="$(_genesis_hidden_dirty_lines "$root")" || return 2
    printf '%s\n%s\n' "$st" "$hidden" | grep -v '^??' | grep -vE "$EPHEMERAL_DIRTY_RE" | grep -v '^$' || true
}

# Porcelain-style lines (" M path", " D path", " T path") for entries flagged
# assume-unchanged or skip-worktree whose working-tree file differs from the index.
# Nothing, at no cost, when no entry is flagged. The comparison runs against a
# scratch copy of the index with the flags cleared, so the real index, flags
# included, is never touched; entries that differ there and not in the real index
# are the hidden ones. Returns 2 when any step cannot be read.
_genesis_hidden_dirty_lines() {
    local root="$1" listing idx tmp seen real rc=0
    # `ls-files -v` tags an assume-unchanged entry with a lowercase letter and a
    # skip-worktree entry with S (s when both). The two flags are cleared in
    # separate calls: given both --no-assume-unchanged and --no-skip-worktree,
    # update-index clears only assume-unchanged (measured, git 2.43). The listing
    # is captured before it is searched: piped into an early-exiting `grep -q`,
    # git dies of SIGPIPE on any listing over a pipe buffer, and under pipefail
    # that read as "status unreadable" (measured: this repo's listing is ~200 KB).
    listing="$(git -C "$root" ls-files -v)" || return 2
    grep -E '^([a-z]|S) ' <<<"$listing" >/dev/null || return 0
    # The scratch index sits beside the real one: same filesystem, and no
    # dependence on a writable TMPDIR. Its name carries this shell's pid, so a
    # caller that kills the shell before the rm below can remove exactly this
    # shell's copy (deploy_health.py's probe does, on a timeout).
    idx="$(git -C "$root" rev-parse --path-format=absolute --git-path index)" || return 2
    tmp="$(mktemp "${idx%/*}/genesis-hidden-index.$$.XXXXXX")" || return 2
    # The two flagged lists are read with their status (a listing that fails
    # must not read as "nothing flagged", which would hide every hidden change).
    local -a lower=() skip=()
    mapfile -d '' -t lower < <(_genesis_flagged_paths "$root" '[a-z]')
    wait $! || rc=2
    mapfile -d '' -t skip < <(_genesis_flagged_paths "$root" '[Ss]')
    wait $! || rc=2
    if [ "$rc" -eq 0 ] && cp "$idx" "$tmp" \
        && { [ "${#lower[@]}" -eq 0 ] || printf '%s\0' "${lower[@]}" \
            | GIT_INDEX_FILE="$tmp" xargs -0 -r git -C "$root" update-index --no-assume-unchanged --; } \
        && { [ "${#skip[@]}" -eq 0 ] || printf '%s\0' "${skip[@]}" \
            | GIT_INDEX_FILE="$tmp" xargs -0 -r git -C "$root" update-index --no-skip-worktree --; }; then
        GIT_INDEX_FILE="$tmp" git -C "$root" update-index -q --refresh >/dev/null 2>&1 || true
        seen="$(GIT_INDEX_FILE="$tmp" git -C "$root" diff-files --name-status --no-renames)" || rc=2
        real="$(git -C "$root" diff-files --name-status --no-renames)" || rc=2
    else
        rc=2
    fi
    rm -f "$tmp" "$tmp.lock"
    [ "$rc" -eq 0 ] || return 2
    # In `seen` and not in `real`: hidden by a flag. Rewritten as porcelain lines.
    printf '%s\n' "$seen" | grep -vxF -f <(printf '%s\n' "$real") \
        | sed -nE 's/^([MDT])\t(.*)$/ \1 \2/p' || true
}

# Does <root> have an entry flagged assume-unchanged or skip-worktree? A refusal
# that lists a hidden change says so: the change is invisible to git status, git
# diff, git stash and git add -p (measured), so the listed path otherwise looks
# unfixable. An unreadable listing answers no.
genesis_has_flagged_entries() {
    local listing
    listing="$(git -C "$1" ls-files -v 2>/dev/null)" || return 1
    grep -E '^([a-z]|S) ' <<<"$listing" >/dev/null
}

# NUL-separated paths of <root>'s index entries whose `ls-files -v` tag matches the
# glob <tag>.
_genesis_flagged_paths() {
    local root="$1" tag="$2" e
    while IFS= read -r -d '' e; do
        # shellcheck disable=SC2254  # <tag> is a glob on purpose
        case "${e%% *}" in $tag) printf '%s\0' "${e#? }" ;; esac
    done < <(git -C "$root" ls-files -v -z)
    wait $!
}

# Does <path> (relative to <root>) hold anything git does not track: an untracked
# or IGNORED file, or a directory with one inside? A tracked file or directory there
# is git's to replace; the merge removes it as the range says. Something present
# that git lists nothing under at all (an empty directory) counts too. An
# unreadable listing counts: refusing is the safe side. Only emptiness is read, so
# the listings are newline-separated: a NUL-separated one inside $(...) makes bash
# warn "ignored null byte" on stderr for every hit.
genesis_untracked_node() {
    local root="$1" path="$2" others tracked
    [ -e "$root/$path" ] || [ -L "$root/$path" ] || return 1
    others="$(git -C "$root" ls-files --others -- "$path" 2>/dev/null)" || return 0
    [ -z "$others" ] || return 0
    tracked="$(git -C "$root" ls-files -- "$path" 2>/dev/null)" || return 0
    [ -z "$tracked" ]
}

# Paths the range <from>...<to> brings in (every change but a deletion) that
# already exist in <root> untracked (or with an untracked file where a parent
# directory goes). Not only additions: when a diverged local branch deleted a
# tracked path and keeps an ignored local copy there, the incoming side's
# MODIFICATION of it is `M` from the merge base, and git writes that version over
# the local file in the modify/delete conflict (measured, git 2.43); the
# conflict path's `merge --abort` then removes it. git refuses to overwrite a
# plain untracked file, but it overwrites an IGNORED one without asking (measured,
# git 2.43), and on a true 3-way merge `--no-overwrite-ignore` does not stop it: a
# local secrets or settings file would be lost. The range is taken from the merge
# base (`...`), so on a fast-forward it is exactly <from>..<to>, and on a diverged
# branch it is what the incoming side changes. A path the local side still
# tracks is git's own to merge, so only untracked ones count.
# NOT covered: a path git INVENTS during the merge. In a file/directory conflict
# the ort strategy moves the local file aside to `<path>~HEAD` (or `~<branch>`),
# and an ignored file already sitting at that name is overwritten. Only incoming
# path names are scanned, so a name git makes up is not; plain update.sh merges
# had the same exposure before this scan existed.
# Prints the colliding paths, one per line. Returns 0 when there are none, 1 when
# there are, 2 when the range cannot be listed.
genesis_range_collisions() {
    _genesis_diff_collisions "$1" "$2...$3"
}

# The same scan over the TWO-DOT diff <from> <to>: exactly the paths a checkout
# (`git switch`) from <from> to <to> writes, which is what a killed switch can
# have left behind. On `live` the old and new tips diverge (each rebuild is a new
# merge chain on a new base), and the symmetric range above then misses every
# path the new tip has back that a dropped candidate had deleted (MEASURED, git
# 2.43: old...new named only the main-side addition; old new named both).
genesis_two_dot_collisions() {
    _genesis_diff_collisions "$1" "$2" "$3"
}

# The paths `git diff --name-only <args>` names, in its order, into the caller's
# array _raw (exact, read NUL-separated) and associative array _shown (each raw
# path -> the path as git prints it: C-style quoted when it holds a newline, a
# control character, a quote or a backslash, so one path is always one display
# line). Every read is checked: either listing failing, or the two disagreeing,
# returns 1, never an empty list that reads as "nothing changed".
_genesis_diff_paths() {
    local root="$1" i
    shift
    local -a shown=()
    mapfile -d '' -t _raw < <(git -C "$root" diff -z --no-renames --name-only "$@" 2>/dev/null)
    wait $! || return 1
    mapfile -t shown < <(git -C "$root" diff --no-renames --name-only "$@" 2>/dev/null)
    wait $! || return 1
    [ "${#_raw[@]}" -eq "${#shown[@]}" ] || return 1
    for i in "${!_raw[@]}"; do
        _shown["${_raw[$i]}"]="${shown[$i]}"
    done
}

# The walk both share; arguments after <root> are the diff's revisions. The
# paths printed are git's display form (_genesis_diff_paths).
_genesis_diff_collisions() {
    local root="$1" collisions="" f p t
    shift
    local -a _raw=()
    local -A _shown=()
    _genesis_diff_paths "$root" --diff-filter=d "$@" || return 2
    # A path in the index can never collide, and the second scan runs while the
    # server is stopped, so the index is read ONCE and those paths skip the two
    # per-path git calls below. An unreadable index (its read checked) leaves the
    # set empty: every path then takes the full check, slower, never less safe.
    local -A tracked=()
    while IFS= read -r -d '' t; do
        tracked["$t"]=1
    done < <(git -C "$root" ls-files -z 2>/dev/null)
    wait $! || tracked=()
    for f in "${_raw[@]}"; do
        [ -n "${tracked[$f]:-}" ] && continue
        if genesis_untracked_node "$root" "$f"; then
            collisions+="${_shown[$f]}"$'\n'
            continue
        fi
        [ -e "$root/$f" ] || [ -L "$root/$f" ] && continue
        p="$f"
        while [ "$p" != "${p%/*}" ]; do
            p="${p%/*}"
            [ -d "$root/$p" ] && [ ! -L "$root/$p" ] && break
            if genesis_untracked_node "$root" "$p"; then
                collisions+="${_shown[$f]}"$'\n'
                break
            fi
        done
    done
    printf '%s' "$collisions"
    [ -z "$collisions" ]
}

# What an update killed in its "merging" phase may have left in <root> while HEAD
# still sits at its rollback commit <rb_commit>: a fast-forward killed mid-write
# leaves HEAD unmoved and the files half new. Prints one line per sign of that, and
# nothing when there is none; never fails (a step it cannot read prints a line
# saying so). Tracked changes count only on the paths the update writes (the
# ephemeral allowlist included: the merge writes those too); a tracked change
# anywhere else is not its doing (an indexer's or another session's edit), which
# the caller judges on its own (#3492). git status cannot see a file whose index
# entry is flagged skip-worktree or assume-unchanged, so the hidden-edit scan
# runs as well. A fast-forward killed after writing only the files the range
# ADDS leaves them untracked, which status cannot show; update.sh's pre-stop
# scan refuses a range that lands on an existing untracked path, so one present
# now appeared during the run. The paths written are main's merge range
# <rb_commit>...<deploy_head> (a fast-forward's is exactly the two-dot one), or
# with a fourth argument --two-dot the two-dot diff <rb_commit> <deploy_head>:
# what a `git switch` between the two writes, the range every residue check on a
# `live` row uses; <rb_commit> is then whichever end HEAD is at. <deploy_head>
# empty: no range, so every tracked change counts and no collision is checked.
# Read by scripts/lib/update_state_guard.py (the one rule bootstrap.sh's crash
# recovery, update.sh, deploy_code_only.sh, the live engine and the dashboard
# ask) and by update.sh's live rollback.
genesis_merge_residue() {
    local root="$1" rb_commit="$2" deploy_head="$3" mode="${4:-}" dirty="" hidden="" new="" rc=0
    local -a range=()
    dirty="$(git -C "$root" status --porcelain --untracked-files=no --no-renames 2>/dev/null)" \
        || dirty="(status unreadable)"
    hidden="$(_genesis_hidden_dirty_lines "$root" 2>/dev/null)" \
        || hidden="(flagged-entry status unreadable)"
    if [ -n "$deploy_head" ]; then
        if [ "$mode" = --two-dot ]; then
            range=("$rb_commit" "$deploy_head")
        else
            range=("$rb_commit...$deploy_head")
        fi
        _genesis_dirt_on_range "$root" "${range[@]}"
    fi
    if [ -n "$deploy_head" ] && [ "$mode" = --two-dot ]; then
        new="$(genesis_two_dot_collisions "$root" "$rb_commit" "$deploy_head" 2>/dev/null)" || rc=$?
        [ "$rc" -ne 2 ] || new="(the checkout's range cannot be listed)"
    elif [ -n "$deploy_head" ]; then
        new="$(genesis_range_collisions "$root" "$rb_commit" "$deploy_head" 2>/dev/null)" || rc=$?
        [ "$rc" -ne 2 ] || new="(the update's incoming range cannot be listed)"
    fi
    printf '%s\n%s\n%s\n' "$dirty" "$hidden" "$new" | grep -v '^$' || true
}

# genesis_merge_residue's filter: keeps, in the caller's $dirty and $hidden, only
# the lines whose path the diff over the given revisions names (deletions
# included). Membership is exact: status is read NUL-separated with renames off,
# and its paths matched against the diff's raw paths; every kept line shows git's
# diff's quoted form (_genesis_diff_paths; unlike `git status`, it leaves a
# trailing space unquoted). A listing
# that fails or cannot be read keeps every line: doubt never shrinks the residue.
_genesis_dirt_on_range() {
    local root="$1" rec kept="" path
    shift
    local -a _raw=() recs=()
    local -A _shown=() quoted=()
    [ "$dirty" != "(status unreadable)" ] || return 0
    _genesis_diff_paths "$root" "$@" || return 0
    mapfile -d '' -t recs < <(git -C "$root" status --porcelain -z --untracked-files=no --no-renames 2>/dev/null)
    wait $! || return 0
    for rec in "${recs[@]}"; do
        path="${rec:3}"
        if [ -n "${_shown[$path]+set}" ]; then kept+="${rec:0:3}${_shown[$path]}"$'\n'; fi
    done
    dirty="$kept"
    # Hidden lines come from diff-files, in git's quoted form already.
    case "$hidden" in "("*) return 0 ;; esac
    for path in "${_raw[@]}"; do
        quoted["${_shown[$path]}"]=1
    done
    kept=""
    while IFS= read -r rec; do
        if [ -n "$rec" ] && [ -n "${quoted[${rec:3}]:-}" ]; then kept+="$rec"$'\n'; fi
    done <<< "$hidden"
    hidden="$kept"
}

# Is <root> still on <branch> at exactly <head> (a full commit id)? Another
# session or editor can switch the branch or commit while a deploy runs, and
# every later git step would then act on THEIR checkout. Sets _now_head and
# _now_branch (empty for a detached HEAD) so the caller can say what moved. An
# unreadable HEAD reads as moved: refusing is the safe side.
genesis_checkout_unmoved() {
    local root="$1" head="$2" branch="$3"
    _now_head="$(git -C "$root" rev-parse -q --verify 'HEAD^{commit}' 2>/dev/null || true)"
    _now_branch="$(git -C "$root" symbolic-ref --short -q HEAD 2>/dev/null || true)"
    [ -n "$_now_head" ] && [ -n "$head" ] && [ "$_now_head" = "$head" ] \
        && [ -n "$branch" ] && [ "$_now_branch" = "$branch" ]
}
