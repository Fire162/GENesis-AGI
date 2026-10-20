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


def outside_fences(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Yield ``(index, line)`` for lines that are NOT inside a fenced block.

    A plan documents a convention by SHOWING it, so the divider and the title
    heading both appear inside ``` examples in perfectly ordinary plans — this
    repo's own reference doc does exactly that. Matching those is how a plan
    truncates itself at its own illustration.

    Fence detection is the CommonMark-ish subset that markdown writers actually
    use: a line whose first non-space run is three or more backticks or tildes
    toggles the state. The info string is ignored, and a closing fence of a
    different character does not close — ``` inside a ~~~ block is content.
    """
    fence_char = ""
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped[:3] in ("```", "~~~"):
            char = stripped[0]
            if not fence_char:
                fence_char = char
                continue
            if char == fence_char:
                fence_char = ""
            continue
        if not fence_char:
            yield idx, line


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

    Known residual: an UNCLOSED fence above the divider hides it, so the whole
    plan reads as live (0 of 66 real plans on the measuring install, 2026-10-19).
    """
    lines = content.splitlines()
    for idx, line in outside_fences(lines):
        if _DIVIDER.match(line):
            return "\n".join(lines[:idx])
    return content
