"""The ONE reader of plan documents for hooks: which plan, and which part is live.

Two hooks read the plan an ExitPlanMode is about, and each used to carry its own
copy of both questions, which disagreed:

* ``adopt_first_gate --plan`` resolved the path from the authoritative
  ``planFilePath`` field, but split the live part with a regex that required the
  decorative ``═`` rule, so it missed a one-``#`` divider and a divider followed by
  text (2 of 66 real plans, MEASURED 2026-10-19), and it split the RAW text, so a
  divider shown inside a fenced example cut the plan short.
* ``plan_bookmark_hook`` read the live part correctly, but never read
  ``planFilePath``: it regex-scanned ``str(tool_input)``, where the full ``plan``
  markdown serializes first, so it returned the first plan path quoted in the
  plan's own prose. With nothing found it took the newest plan on the box, from
  any session. MEASURED 2026-10-19: 243 of 243 ExitPlanMode tool inputs across 60
  recent transcripts carry ``planFilePath``.

So this module holds one copy of each answer, taken from the hook that had it
right. Stdlib only, plus the sibling ``hook_input`` — the one place that knows
the payload contract.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import tool_input  # noqa: E402

_BLOB_PLAN_PATH = re.compile(r"(/[^\s\"']+\.claude/plans/[^\s\"']+\.md)")

#: A divider is a heading whose text BEGINS with the phrase, after an optional
#: decorative rule: `## ═══ SUPERSEDED BELOW ═══`, `# ═══ SUPERSEDED BELOW — …`,
#: `## SUPERSEDED BELOW`. A heading that merely MENTIONS the phrase (a plan titled
#: "teach hooks the SUPERSEDED BELOW divider") is not one: matching it would empty
#: the live section, and the adopt-first gate fails open on an empty plan.
_DIVIDER = re.compile(r"^#+\s*(?:═+\s*)?SUPERSEDED BELOW")

#: A fence delimiter (after stripping): the run, then any info string.
_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")


def plan_path(payload: dict) -> str:
    """The plan file this ExitPlanMode is about, or "" when it cannot be known.

    The AUTHORITATIVE field first: ``planFilePath`` is in every real payload
    (counts in the module docstring). The blob scan is a fallback for older
    shapes only, and it is a fallback for a reason: ``plan`` (the full markdown)
    serializes BEFORE ``planFilePath``, so a first-match scan can return a path
    quoted inside the plan's own prose.

    With no path in the payload, never guess another session's plan: the only
    honest correlation is the session id in the filename, and absent that, a
    single unambiguous file is the only safe pick.
    """
    for key in ("planFilePath", "plan_file_path"):
        direct = tool_input(payload).get(key) or payload.get(key)
        if isinstance(direct, str) and direct.endswith(".md"):
            return direct
    # Older shapes: the path the TOOL returned before any path the plan's prose
    # quotes (the bookmark always searched the output first). Claude Code's
    # ExitPlanMode result is {plan, isAgent, filePath} with the plan text FIRST
    # (MEASURED 2026-10-21, 3 of 3 recent results), so read its path field, then
    # scan the result WITHOUT its plan text. Read straight from the payload, not
    # hook_input.tool_response, whose legacy env fallback can hold another
    # tool's result.
    for source in (payload.get("tool_response"), payload.get("tool_output")):
        if isinstance(source, str):
            with contextlib.suppress(ValueError):
                source = json.loads(source)
        if isinstance(source, dict):
            for key in ("filePath", "planFilePath", "plan_file_path"):
                value = source.get(key)
                if isinstance(value, str) and value.endswith(".md"):
                    return value
            source = {k: v for k, v in source.items() if k != "plan"}
        if source:
            text = source if isinstance(source, str) else json.dumps(source)
            match = _BLOB_PLAN_PATH.search(text)
            if match:
                return match.group(1)
    match = _BLOB_PLAN_PATH.search(json.dumps(payload))
    if match:
        return match.group(1)
    plans = Path.home() / ".claude" / "plans"
    if plans.is_dir():
        try:
            candidates = sorted(plans.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            sid = str(payload.get("session_id") or "")
            if sid:
                own = [p for p in candidates if sid in p.name]
                if own:
                    return str(own[0])
            if len(candidates) == 1:
                return str(candidates[0])
        except OSError:
            pass
    return ""


def _fence_mask(lines: list[str]) -> tuple[list[bool], bool]:
    """Which lines are inside (or delimit) a fenced block, and whether every
    fence closed.

    CommonMark's fence rules, as ``git_push_guard``'s review-body parser applies
    them: a delimiter is a run of three or more backticks or tildes with at most
    three leading spaces. A closing fence uses the opener's character, is at
    least as long, and has no info string, so a ```md line inside a ````
    block, or ``` inside ~~~, is content. An unclosed fence runs to the end.
    """
    mask = [False] * len(lines)
    fence_char, fence_len = "", 0
    for idx, line in enumerate(lines):
        indent = len(line) - len(line.lstrip(" "))
        fence = _FENCE.match(line.strip()) if indent <= 3 else None
        if fence:
            run, info = fence.group(1), fence.group(2).strip()
            if not fence_char:
                fence_char, fence_len = run[0], len(run)
                mask[idx] = True
                continue
            if run[0] == fence_char and len(run) >= fence_len and not info:
                fence_char, fence_len = "", 0
                mask[idx] = True
                continue
        mask[idx] = bool(fence_char)
    return mask, not fence_char


def outside_fences(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Yield ``(index, line)`` for lines that are NOT inside a fenced block.

    A plan documents a convention by SHOWING it, so the divider and the title
    heading both appear inside ``` examples in perfectly ordinary plans — this
    repo's own reference doc does exactly that. Matching those is how a plan
    truncates itself at its own illustration.
    """
    mask, _ = _fence_mask(lines)
    for idx, line in enumerate(lines):
        if not mask[idx]:
            yield idx, line


def strip_fences(text: str) -> str:
    """``text`` with every fenced line blanked (line count kept): quoted
    material, not proposals or verdicts. The one fence parser every plan hook
    uses, so the live section and the gate's searches cannot disagree."""
    lines = text.split("\n")
    mask, _ = _fence_mask(lines)
    return "\n".join("" if m else line for m, line in zip(mask, lines, strict=True))


def live_section(content: str) -> str:
    """The part of a plan above its superseded-content divider.

    The convention is documented in the genesis-development skill
    (`references/plan-docs.md`): a heading containing SUPERSEDED BELOW divides
    live plan content from archaeology kept for provenance. No divider means
    the whole document is live, which is also the correct reading for every
    plan written before the convention existed.

    Matched on the WORDS at the start of a heading (``_DIVIDER``), not on the
    decorative rule the template draws around them — the box characters are
    ornament and a plan that omits them still means it. Anchored at column zero
    on a `#` heading so the phrase quoted inside a paragraph cannot truncate the
    document, and skipped inside FENCED blocks so a plan that documents the
    convention by showing it does not truncate itself at its own example.

    Two readings fail CLOSED for the adopt-first gate (more of the plan stays
    live, never less): a divider inside an HTML comment does not divide, and a
    document whose FIRST heading begins with the phrase is a plan about the
    divider, not one. When a fence never closes the fence reading cannot be
    trusted, so the first divider heading anywhere divides, as before this
    reader existed: hiding it would let an old verdict below it clear a new
    proposal. Lines are split on newline only: ``splitlines()`` also breaks at
    U+2028, which Markdown does not.
    """
    lines = content.split("\n")
    mask, closed = _fence_mask(lines)
    in_comment = False
    first_heading = True
    for idx, line in enumerate(lines):
        if in_comment:
            in_comment = "-->" not in line
            continue
        if not closed or not mask[idx]:
            if line.lstrip().startswith("<!--") and "-->" not in line:
                in_comment = True
                continue
            if line.startswith("#"):
                if _DIVIDER.match(line) and not first_heading:
                    return "\n".join(lines[:idx])
                first_heading = False
    return content
