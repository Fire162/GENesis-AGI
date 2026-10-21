"""Whether ~/.genesis/update_state.json still guards merged code (#3139).

update.sh writes update_state.json at each phase. An update that merged code it
has not activated (a crash, a refused or interrupted ``--post-merge``) leaves it
behind, and so does a merge-conflict exit, whose resolver needs its rollback
tag. deploy_code_only.sh and the live engine refuse to restart or move the
checkout while such a file stands, and update.sh and the dashboard refuse to
overwrite or delete it. All of them ask this module, so the shell and the
dashboard cannot disagree.

Verdicts, in order:

1. no file: SAFE;
2. a file that cannot be read or parsed, or is not a JSON object: GUARDS;
3. phase "done": SAFE (written just before update.sh deletes the file);
4. phase "fetching": SAFE only when the rollback tag is HEAD, the state names
   the commit it fetched (``deploy_head``), and it says a plain run wrote it
   (``post_merge`` exactly false). A ``--post-merge`` run writes "fetching" over
   code that is already merged, sometimes with a fallback tag made at that
   merged HEAD, and a state without the key (older, or hand-edited) cannot rule
   that out; else GUARDS;
5. phase "merging" with the rollback tag at HEAD, no MERGE_HEAD, and no merge
   residue (``genesis_merge_residue`` in deploy_checkout.sh; it counts tracked
   changes, ephemeral paths included, on the paths the merge
   ``rollback...deploy_head`` writes, and every tracked change when the row
   names no ``deploy_head``): CONFLICT_PENDING when update_conflicts.json names
   the same ``deploy_head`` (update.sh writes it only for a real conflict),
   otherwise SAFE (a run killed before its merge). With residue, MIXED_TREE: the
   checkout holds part of a half-written update. A tracked change only outside
   the merge's paths is someone else's edit: GUARDS, as before #3492;
6. a "merging" row carrying the live engine's move record ``live_move``
   ({"to": <tip>, "phase": ref|moving|partial|clean|done}, which the engine
   writes into update.sh's own row before and after each move, #3492). Every
   residue there is the TWO-DOT diff a `git switch` writes, never main's
   symmetric range: on `live` the old and new tips diverge. HEAD at the tag:
   MIXED_TREE when the record says "partial" or the two-dot residue toward
   ``to`` is non-empty (a "clean" record, git having refused and written
   nothing, counts tracked changes only), else SAFE. HEAD on `live` at ``to``:
   MIXED_TREE when the two-dot residue back toward the tag is non-empty (a
   rollback checkout killed partway), else GUARDS (moved, not bootstrapped).
   Only tracked changes on the switch's own paths count there: one anywhere
   else is someone else's edit, which GUARDS, not MIXED_TREE. The remedy
   follows HEAD: at the tag, put the paths back and Dismiss; at the tip, put
   them back and finish with --post-merge. A record of any other shape, or
   one under any phase but "merging", GUARDS;
6a. a "merging" row on `live` with no record and an own_head other than the
   tag (update.sh adopted the tip, and its row no longer carries the record)
   is judged the same way toward own_head, and that verdict is final (main's
   rule 5 would judge the upstream base range, which no live switch writes);
7. every other "merging", any other phase, or none: GUARDS.

The pid is not an input: callers answer liveness first (update.lock in the
shell, env.update_in_progress() in the dashboard). Anything this module cannot
decide GUARDS. Retiring a guarding file on purpose is the job of that update's
``--post-merge``, bootstrap's crash recovery, or the dashboard's Dismiss.

Command line (``python3 -I -S``, standard library only)::

    update_state_guard.py <state-file> <repo> [<conflicts-file>]

Exit 10 when the file guards nothing, 0 when it guards, 3 when it is a pending
merge conflict, 4 when the checkout is half-written (MIXED_TREE: the message
lists the paths, and ``--post-merge`` refuses it); the remedy is printed on
stdout. A state too deeply nested to parse GUARDS (exit 0: it cannot be read).
Any other failure inside the check (git or bash not runnable, a git call timing
out) exits 1: it judged nothing, so it joins the "could not run" class with
Python's own 1 on an uncaught exception, 2 on a usage error, and a shell's
126/127. Callers keep the state on every exit but 10, and --post-merge (which
finishes a guarding 0) refuses 1, 2, 126 and 127 as it refuses 4. Only exit 10
lets a caller go on.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

SAFE = "safe"
GUARDS = "guards"
CONFLICT_PENDING = "conflict_pending"
MIXED_TREE = "mixed_tree"

# SAFE is 10, a code no default failure produces: Python exits 1 on an uncaught
# exception and 2 on a usage error, a shell 126/127 when it cannot run the file,
# so none of those can ever read as "nothing to guard".
EXIT_CODES = {GUARDS: 0, SAFE: 10, CONFLICT_PENDING: 3, MIXED_TREE: 4}

#: The engine's move phases, as deploy_candidates_plan.py writes them.
MOVE_PHASES = ("ref", "moving", "partial", "clean", "done")
_HEX = frozenset("0123456789abcdef")
#: Shown as "?" in a residue path (N5): terminal control, tabs, DEL.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_LIB = os.path.dirname(os.path.abspath(__file__))


# The dashboard runs this while it holds update.lock in a request thread, so a
# hung git (a wedged filesystem, a frozen cgroup) must not hold the lock for good.
# 120 s matches the dashboard's other checks; TimeoutExpired reaches every
# caller's exception path, which keeps the state (guards).
_TIMEOUT_S = 120


def _env() -> dict[str, str]:
    # Local reads only; GIT_* from the caller would point git elsewhere.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _rev(repo: str, rev: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "-q", rev],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=_TIMEOUT_S,
    ).stdout.strip()


def _tag_commit(state: dict, repo: str) -> str:
    """The rollback tag's commit ("" when it names none). The value must be a
    valid tag NAME (git check-ref-format, as bootstrap.sh checks it): "pre~0" or
    "pre^{commit}" is a revision expression, which would resolve to a commit no
    such tag points at."""
    tag = state.get("rollback_tag")
    if not isinstance(tag, str) or not tag or tag.startswith("-"):
        return ""
    ok = subprocess.run(
        ["git", "-C", repo, "check-ref-format", f"refs/tags/{tag}"],
        capture_output=True,
        env=_env(),
        timeout=_TIMEOUT_S,
    )
    if ok.returncode != 0:
        return ""
    return _rev(repo, f"refs/tags/{tag}^{{commit}}")


def _tag_commit_is_head(state: dict, repo: str) -> str | None:
    """The rollback tag's commit when it is HEAD, else None."""
    tagged = _tag_commit(state, repo)
    head = _rev(repo, "HEAD^{commit}")
    return tagged if tagged and tagged == head else None


def _move_record(state: dict) -> tuple[str, str] | None:
    """(to, phase) of a well-formed ``live_move``; None for any other shape."""
    rec = state.get("live_move")
    if not isinstance(rec, dict):
        return None
    to, phase = rec.get("to"), rec.get("phase")
    if not (isinstance(to, str) and len(to) in (40, 64) and set(to) <= _HEX):
        return None
    return (to, phase) if phase in MOVE_PHASES else None


def _merge_residue(repo: str, rb_commit: str, deploy_head: str, two_dot: bool = False) -> str:
    """genesis_merge_residue's lines; any failure to run it is residue."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1/deploy_checkout.sh" && genesis_merge_residue "$2" "$3" "$4" ${5:+"$5"}',
            "update_state_guard",
            _LIB,
            repo,
            rb_commit,
            deploy_head,
            "--two-dot" if two_dot else "",
        ],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=_TIMEOUT_S,
    )
    if proc.returncode != 0:
        return f"(the merge residue check failed: exit {proc.returncode})"
    return proc.stdout.strip()


def _conflict_recorded(conflicts_path: str, deploy_head: str) -> bool:
    """Does the conflict record name this commit? A record that exists but
    cannot be read, parsed, or is not an object counts as naming it: doubt keeps
    the conflict the resolver is waiting on."""
    if not os.path.lexists(conflicts_path):
        return False
    try:
        with open(conflicts_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return True
    if not isinstance(data, dict):
        return True
    return data.get("deploy_head") == deploy_head


#: How a MIXED_TREE reason starts when HEAD is on `live` at the rebuilt tip:
#: restoring the listed paths to HEAD then puts the FORWARD tree back, which
#: --post-merge finishes (message() reads it to give that remedy).
_FORWARD = "the checkout is at the rebuilt live tip"


def _mixed(tag: str, to: str, residue: str, *, forward: bool = False) -> tuple[str, str]:
    """MIXED_TREE, its reason naming the two commits and listing every path.
    ``forward``: HEAD is at ``to``, not the tag, so the remedy finishes forward."""
    lines = residue or "(the rebuild recorded that git wrote part of the tree)"
    # Paths reach terminals and a supervising session's summary verbatim: a
    # control character in a file name (ESC, a tab) is shown as "?".
    # Split on "\n" only: str.splitlines also breaks on \x1c-\x1e, \v and \f.
    lines = "\n".join(_CONTROL.sub("?", ln) for ln in lines.split("\n"))
    where = (
        f"{_FORWARD} {to[:12]}, half-written back toward {tag[:12]}"
        if forward
        else f"the checkout is half-written between {tag[:12]} and {to[:12] or 'its merge'}"
    )
    return MIXED_TREE, where + ":\n" + "\n".join("  " + ln for ln in lines.split("\n"))


def _live_verdict(state: dict, repo: str, to: str, move_phase: str) -> tuple[str, str]:
    """A live row judged toward ``to``: the engine's record (rule 6), or the
    adopted tip once update.sh dropped the record (own_head, rule 6a). Only the
    paths a switch between the tag and ``to`` writes count as residue; a
    tracked change anywhere else is someone else's edit, which guards (as it
    always has) but is not a half-written tree."""
    tag = _tag_commit(state, repo)
    head = _rev(repo, "HEAD^{commit}")
    if not tag or not head:
        return GUARDS, "its rollback tag or HEAD cannot be resolved"
    if head == tag:
        # "clean": the engine compared the tree before and after, and git wrote
        # nothing, so nothing on disk is the move's.
        if move_phase != "clean":
            residue = _merge_residue(repo, tag, to, two_dot=True)
            if residue or move_phase == "partial":
                return _mixed(tag, to, residue)
        if _merge_residue(repo, tag, ""):
            return GUARDS, "tracked files changed that the move does not write"
        return SAFE, "the rebuild moved nothing"
    branch = subprocess.run(
        ["git", "-C", repo, "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=_TIMEOUT_S,
    ).stdout.strip()
    if head == to and branch == "refs/heads/live":
        residue = _merge_residue(repo, to, tag, two_dot=True)
        if residue:
            return _mixed(tag, to, residue, forward=True)
        return GUARDS, f"it moved live to {to[:12]}, which bootstrap has not run on"
    return GUARDS, "the checkout is neither at its rollback tag nor at the rebuilt live tip"


def _adopted_tip(state: dict, repo: str) -> str | None:
    """Rule 6a: a "merging" row on `live` with no record, whose own_head is a
    commit other than the tag's, is the row update.sh wrote after adopting the
    rebuilt tip (the record goes with that write). Its rollback can still be
    killed partway, so the row is judged toward own_head as a record would be."""
    if state.get("original_branch") != "live" or "live_move" in state:
        return None
    own = state.get("own_head")
    if not (isinstance(own, str) and len(own) in (40, 64) and set(own) <= _HEX):
        return None
    tag = _tag_commit(state, repo)
    return own if tag and own != tag else None


def state_verdict(state: object, repo: str, conflicts_path: str | None = None) -> tuple[str, str]:
    """The rule for an already-parsed state. Returns (verdict, reason)."""
    if not isinstance(state, dict):
        return GUARDS, "it cannot be read"
    phase = state.get("phase")
    if "live_move" in state:
        if phase != "merging":
            return GUARDS, f"it carries a live move record in phase {phase!r}"
        move = _move_record(state)
        if move is None:
            return GUARDS, "its live move record cannot be read"
        if _rev(repo, "MERGE_HEAD"):
            return GUARDS, "a merge is in progress in the checkout"
        return _live_verdict(state, repo, *move)
    if phase == "done":
        return SAFE, "it finished"
    deploy_head = state.get("deploy_head")
    deploy_head = deploy_head if isinstance(deploy_head, str) else ""
    if phase == "fetching":
        # Only a state a plain run wrote (post_merge exactly False) can be safe: a
        # --post-merge, a state from before the key existed, or a hand edit may
        # be "fetching" over merged code at a fallback tag made at that HEAD.
        if state.get("post_merge") is not False:
            return GUARDS, "it may be a --post-merge run that stopped before bootstrap"
        if deploy_head and _tag_commit_is_head(state, repo):
            return SAFE, "it stopped before merging anything"
        return GUARDS, "phase 'fetching', but HEAD is not its rollback tag"
    if phase == "merging":
        adopted = _adopted_tip(state, repo)
        if adopted and not _rev(repo, "MERGE_HEAD"):
            # Final: main's rule 5 would judge the upstream base range
            # (deploy_head), which is not what a switch between the two live
            # tips writes (#3532 round 1).
            return _live_verdict(state, repo, adopted, "done")
        rb_commit = _tag_commit_is_head(state, repo)
        if not rb_commit:
            return GUARDS, "it merged code bootstrap has not run on"
        if _rev(repo, "MERGE_HEAD"):
            return GUARDS, "a merge is in progress in the checkout"
        residue = _merge_residue(repo, rb_commit, deploy_head)
        if residue:
            return _mixed(rb_commit, deploy_head, residue)
        # A tracked change the merge never writes (residue counts only the
        # merge's paths): someone else's edit. It guards, as it did before
        # #3492, but is not a half-written tree, so --post-merge may finish.
        if deploy_head and _merge_residue(repo, rb_commit, ""):
            return GUARDS, "tracked files changed while it was merging"
        if deploy_head and conflicts_path and _conflict_recorded(conflicts_path, deploy_head):
            return CONFLICT_PENDING, "it stopped on merge conflicts; nothing was merged"
        return SAFE, "it stopped before merging anything"
    if isinstance(phase, str) and phase:
        return GUARDS, f"it stopped in phase '{phase}'"
    return GUARDS, "it names no phase"


def verdict(state_path: str, repo: str, conflicts_path: str | None = None) -> tuple[str, str]:
    """The rule for the file on disk."""
    if not os.path.lexists(state_path):
        return SAFE, "there is no state file"
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return GUARDS, "it cannot be read"
    if conflicts_path is None:
        conflicts_path = os.path.join(
            os.path.dirname(os.path.abspath(state_path)), "update_conflicts.json"
        )
    return state_verdict(state, repo, conflicts_path)


def message(state_path: str, verdict_: str, reason: str) -> str:
    """What every refusing caller says, so the remedy reads the same everywhere."""
    if verdict_ == MIXED_TREE:
        # The one place the paths are printed from (#3492): callers print this.
        # Restoring to HEAD gives back whichever tree HEAD is: the code that ran
        # before at the tag, the rebuilt tip at it, so the last step differs.
        head = (
            f"{state_path} records an update that stopped partway: {reason}\n"
            "These paths hold part of an update's checkout. They may also include another "
            "session's edits: save any you want to keep (copy them aside) before restoring "
            "anything. Then restore tracked paths to HEAD with git restore --source=HEAD "
            "--staged --worktree -- <path>, delete the listed untracked files, and remove a "
            "stale .git/index.lock if no git process is running. "
        )
        if reason.startswith(_FORWARD):
            return head + (
                "HEAD is the rebuilt tip, so the checkout then holds that tree: finish with "
                "scripts/update.sh --post-merge (it passes once these paths are back). "
                "Dismissing the update instead (the dashboard's update card) leaves a checkout "
                "holding code bootstrap never ran on, with nothing to stop a restart onto it."
            )
        return head + (
            "HEAD is the code that ran before the update, so the checkout is then that code "
            "again: Dismiss the update on the dashboard's update card (or remove "
            "update_state.json). Do NOT run scripts/update.sh --post-merge: it refuses a "
            "half-written tree."
        )
    if verdict_ == CONFLICT_PENDING:
        return (
            f"{state_path} records an earlier update that stopped on merge conflicts; "
            "nothing was merged. Resolve them (Resolve with Opus on the dashboard's "
            "update card, or merge the deploy_head in update_conflicts.json and run "
            "scripts/update.sh --post-merge), or Dismiss it on the dashboard's update card."
        )
    return (
        f"{state_path} records an earlier update that has not finished ({reason}). "
        "Finish it with scripts/update.sh --post-merge, or Dismiss it on the "
        "dashboard's update card."
    )


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(
            "usage: update_state_guard.py <state-file> <repo> [<conflicts-file>]", file=sys.stderr
        )
        return 2
    state_path, repo = argv[0], argv[1]
    try:
        v, reason = verdict(state_path, repo, argv[2] if len(argv) == 3 else None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - any failure to decide could not run
        # Exit 1, not GUARDS' 0: a check that failed partway judged nothing,
        # and --post-merge finishes a guarding state, so 0 would let it go past
        # a half-written tree nobody looked at (#3492). Every caller proceeds
        # only on 10, and --post-merge refuses 1.
        print(
            f"{state_path} could not be checked ({type(exc).__name__}); it is kept. Retry once "
            "the check can run (scripts/update.sh --post-merge refuses until then), or Dismiss "
            "it on the dashboard's update card."
        )
        return 1
    if v != SAFE:
        print(message(state_path, v, reason))
    return EXIT_CODES[v]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
