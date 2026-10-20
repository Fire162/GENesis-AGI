"""The shared plan reader, and the two hooks that read plans through it.

`scripts/hooks/plan_doc.py` answers two questions for every hook that reads the
plan an ExitPlanMode is about: WHICH plan file, and WHICH PART of it is live.
Before it existed `adopt_first_gate` and `plan_bookmark_hook` each answered both,
and disagreed. Each test below pins one answer, and the hook-level tests drive
the REAL payload shape (`plan` + `planFilePath`, measured on 243 of 243 live
ExitPlanMode inputs) through each hook, so a hook that stops calling the reader
fails here rather than passing on a convenient shape.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_GATE = _SCRIPTS / "hooks" / "adopt_first_gate.py"
_BOOKMARK = _SCRIPTS / "plan_bookmark_hook.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def plan_doc():
    return _load("plan_doc_under_test", _SCRIPTS / "hooks" / "plan_doc.py")


@pytest.fixture(scope="module")
def bookmark():
    return _load("plan_bookmark_hook_under_test", _BOOKMARK)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME, so no fallback can ever read the developer's real plans."""
    h = tmp_path / "home"
    (h / ".claude" / "plans").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def _write_plan(home: Path, name: str, text: str) -> Path:
    p = home / ".claude" / "plans" / name
    p.write_text(text, encoding="utf-8")
    return p


def _real_payload(path: Path, **extra) -> dict:
    """The shape Claude Code actually sends: `plan` serializes BEFORE `planFilePath`."""
    return {
        "tool_name": "ExitPlanMode",
        "tool_input": {"plan": path.read_text(encoding="utf-8"), "planFilePath": str(path)},
        **extra,
    }


# ── which plan ───────────────────────────────────────────────────────────────
def test_the_authoritative_field_wins_over_a_path_quoted_in_the_plan(plan_doc, home):
    other = _write_plan(home, "other.md", "# Other\n")
    mine = _write_plan(home, "mine.md", f"# Mine\n\nSee {other} for background.\n")
    assert plan_doc.plan_path(_real_payload(mine)) == str(mine)


def test_without_a_path_the_sessions_own_plan_is_found(plan_doc, home):
    _write_plan(home, "a-plan.md", "# A\n")
    own = _write_plan(home, "work-sess1234.md", "# Mine\n")
    assert plan_doc.plan_path({"session_id": "sess1234"}) == str(own)


def test_without_a_path_another_sessions_plan_is_never_guessed(plan_doc, home):
    _write_plan(home, "a.md", "# A\n")
    _write_plan(home, "b.md", "# B\n")
    assert plan_doc.plan_path({"session_id": "nomatch"}) == ""


def test_a_single_unambiguous_plan_is_the_safe_pick(plan_doc, home):
    only = _write_plan(home, "only.md", "# Only\n")
    assert plan_doc.plan_path({}) == str(only)


# ── which part ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "divider",
    [
        "## ═══ SUPERSEDED BELOW ═══",
        "# ═══ SUPERSEDED BELOW — resume block kept for history",
        "## ═══ SUPERSEDED BELOW ═══ (rev 1, kept for provenance)",
        "## SUPERSEDED BELOW",
    ],
)
def test_each_divider_spelling_in_real_plans_divides(plan_doc, divider):
    text = f"# Plan\n\nlive part\n\n{divider}\n\nold part\n"
    live = plan_doc.live_section(text)
    assert "live part" in live and "old part" not in live


def test_no_divider_means_the_whole_plan_is_live(plan_doc):
    text = "# Plan\n\nall of it\n"
    assert plan_doc.live_section(text) == text


def test_a_divider_shown_inside_a_fence_does_not_divide(plan_doc):
    text = "# Plan\n\n```\n## ═══ SUPERSEDED BELOW ═══\n```\n\nstill live\n"
    assert "still live" in plan_doc.live_section(text)


def test_the_divider_phrase_in_prose_does_not_divide(plan_doc):
    text = "# Plan\n\nWe keep a SUPERSEDED BELOW divider here.\n\nstill live\n"
    assert "still live" in plan_doc.live_section(text)


def test_a_heading_that_mentions_the_phrase_is_not_the_divider(plan_doc):
    """A plan ABOUT the divider must not lose its whole body to its own title."""
    text = "# Plan: teach hooks the SUPERSEDED BELOW divider\n\nall of it is live\n"
    assert "all of it is live" in plan_doc.live_section(text)


def test_a_section_merely_labelled_superseded_is_not_the_divider(plan_doc):
    """Long plans keep superseded REVISIONS above the real divider, labelled as such."""
    text = "# Plan\n\n## ═══ (superseded) rev 2 ═══\n\nkept above\n"
    assert "kept above" in plan_doc.live_section(text)


# ── through the hooks ────────────────────────────────────────────────────────
def test_bookmark_records_the_plan_that_was_approved(bookmark, home):
    other = _write_plan(home, "other.md", "# Other plan\n")
    mine = _write_plan(home, "mine.md", f"# My plan\n\nBuilds on {other}.\n")
    path, title = bookmark._extract_plan_info(_real_payload(mine))
    assert (path, title) == (str(mine), "My plan")


def test_bookmark_never_takes_another_sessions_newest_plan(bookmark, home):
    _write_plan(home, "a.md", "# A\n")
    _write_plan(home, "b.md", "# B\n")
    assert bookmark._extract_plan_info({"tool_input": {}, "session_id": "nomatch"}) == ("", "")


def test_bookmark_names_the_session_that_exited_plan_mode(bookmark, monkeypatch):
    # Pinned: a dispatched session running this suite exports GENESIS_CC_SESSION=1,
    # which the hook deliberately skips (test below).
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    assert bookmark._session_hint({"session_id": "mine-123"}) == "mine-123"


def test_background_sessions_are_not_bookmarked(bookmark, monkeypatch):
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    assert bookmark._session_hint({"session_id": "mine-123"}) == ""


def _gate(payload: dict, home: Path, cwd: Path) -> subprocess.CompletedProcess:
    body = {"hook_event_name": "PreToolUse", "session_id": "test", "cwd": str(cwd), **payload}
    return subprocess.run(
        [sys.executable, str(_GATE), "--plan"],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        timeout=30,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "src" / "genesis").mkdir(parents=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(args, cwd=r, check=True, capture_output=True)
    return r


@pytest.mark.parametrize(
    "divider",
    [
        "# ═══ SUPERSEDED BELOW — resume block kept for history",
        "## ═══ SUPERSEDED BELOW ═══ (rev 1, kept for provenance)",
    ],
)
def test_the_gate_ignores_old_proposals_below_any_real_divider(home, repo, divider):
    """A new source file proposed only in archaeology must not block today's exit."""
    plan = _write_plan(
        home,
        "plan.md",
        f"# Plan\n\nFix a typo in docs.\n\n{divider}\n\nAdd src/genesis/brand_new.py\n",
    )
    result = _gate(_real_payload(plan), home, repo)
    assert result.returncode == 0, result.stderr


def test_the_gate_still_blocks_a_live_proposal_above_the_divider(home, repo):
    plan = _write_plan(
        home,
        "plan.md",
        "# Plan\n\nAdd src/genesis/brand_new.py\n\n# ═══ SUPERSEDED BELOW\n\nold\n",
    )
    result = _gate(_real_payload(plan), home, repo)
    assert result.returncode == 2 and "adopt-first" in result.stderr


def test_a_fenced_divider_example_does_not_hide_a_live_proposal(home, repo):
    plan = _write_plan(
        home,
        "plan.md",
        "# Plan\n\n```\n## ═══ SUPERSEDED BELOW ═══\n```\n\nAdd src/genesis/brand_new.py\n",
    )
    result = _gate(_real_payload(plan), home, repo)
    assert result.returncode == 2 and "adopt-first" in result.stderr
