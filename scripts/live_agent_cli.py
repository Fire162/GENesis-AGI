#!/usr/bin/env python3
"""Drive the Genesis live agent from a terminal (Phase 0 host).

Type a line to send a turn; while a turn runs, a new line steers it.
Commands: /i interrupt · /y ID allow · /n ID deny · /s status · /q quit.

    ~/genesis/.venv/bin/python scripts/live_agent_cli.py [--steer priority|interrupt]

The session id is kept in ``~/.genesis/live_agent/state.json`` and resumed on
the next start; a stale id starts a fresh session.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from genesis.env import genesis_home, repo_root  # noqa: E402
from genesis.live_agent import LiveAgent  # noqa: E402
from genesis.live_agent.agent import owner_environ  # noqa: E402

_PREFIX = {
    "progress": "  ·",
    "approval": "  ? approve with /y ID or /n ID:",
    "session": "  [session]",
    "rate_limited": "  [rate limit]",
    "finished": "  ✓",
    "error": "  ! ",
}


async def _print_events(agent: LiveAgent) -> None:
    async for event in agent.events():
        print(f"{_PREFIX.get(event.kind, '  ')} {event.text}", flush=True)


async def _command(agent: LiveAgent, line: str) -> bool:
    """Run one input line; False means quit."""
    cmd, _, arg = line.partition(" ")
    if cmd == "/q":
        return False
    if cmd == "/i":
        await agent.interrupt()
    elif cmd in ("/y", "/n"):
        if not agent.answer(arg.strip(), allow=cmd == "/y"):
            print(f"  no pending approval {arg.strip()!r}")
    elif cmd == "/s":
        print(json.dumps(agent.status(), indent=2))
    else:
        handle = await agent.turn(line)
        print(f"  → {'steered' if handle.steered else 'sent'} ({handle.id})")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--steer", choices=("priority", "interrupt"), default="priority")
    args = parser.parse_args()
    # This process becomes the owner's: the agent's child inherits it.
    clean = owner_environ(dict(os.environ))
    os.environ.clear()
    os.environ.update(clean)

    agent = LiveAgent(
        cwd=repo_root(),
        state_path=genesis_home() / "live_agent" / "state.json",
        steer=args.steer,
    )
    await agent.start()
    printer = asyncio.create_task(_print_events(agent))
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            if not line.strip():
                continue
            try:
                if not await _command(agent, line.strip()):
                    break
            except Exception as exc:  # report and keep the host up (refusals, timeouts)
                print(f"  ! {type(exc).__name__}: {exc}", flush=True)
    finally:
        printer.cancel()
        await agent.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
